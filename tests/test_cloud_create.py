"""Actual HTTP lost replies and explicit retries; phone allocation is simulated."""
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from phone_harness import cloud

KEY = 'pck_fixture_owner'
REQUEST = 'fixture-request-key-1234'


class Service:
    def __init__(self):
        self.requests, self.sessions = [], {}
        self.supported, self.lose_reply, self.bad_echo = True, False, False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                account = self.headers.get('Authorization')
                key = self.headers.get('Idempotency-Key')
                owner.requests.append((self.command, self.path, account, key, body))
                if account not in ('Bearer ' + KEY, 'Bearer other-owner'):
                    return self.reply(401, {'error': 'account required'})
                if self.path == '/me':
                    return self.reply(200, {'session_create_idempotency': 'v1'} if owner.supported else {})
                if self.path == '/sessions' and self.command == 'POST':
                    identity = (account, key or str(len(owner.requests)))
                    parsed = json.loads(body)
                    if identity in owner.sessions:
                        session, original = owner.sessions[identity]
                        if original != parsed:
                            return self.reply(409, {'error': 'request parameters conflict'})
                        if session['state'] == 'ended':
                            return self.reply(410, {**session, 'error': 'request completed'})
                    else:
                        session = {'id': f'phone-{len(owner.sessions)+1}', 'state': 'ready', 'provider': 'fixture'}
                        if key: session['request_key'] = key
                        owner.sessions[identity] = session, parsed
                    if owner.lose_reply:
                        owner.lose_reply = False
                        self.close_connection = True
                        return
                    return self.reply(202, {**session, **({'request_key': 'wrong-key'} if owner.bad_echo else {})})
                if self.path.startswith('/sessions/requests/'):
                    entry = owner.sessions.get((account, self.path.rsplit('/', 1)[1]))
                    return self.reply(200, entry[0]) if entry else self.reply(404, {'error': 'unknown request'})
                entry = next((s for (a, _), (s, _) in owner.sessions.items() if a == account and self.path == '/sessions/' + s['id']), None)
                if not entry: return self.reply(404, {'error': 'unknown session'})
                if self.command == 'DELETE': entry.update(state='ended', cleanup_complete=True)
                self.reply(200, entry)
            def reply(self, status, value):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = do_DELETE = handle_request
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)

    def posts(self): return [r for r in self.requests if r[0] == 'POST']


class Creation(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.addCleanup(self.service.close)
        self.environment = patch.dict(os.environ, {'PHONE_CLOUD_URL': self.service.base, 'PHONE_CLOUD_TOKEN': KEY, 'PHONE_CLOUD_SESSION': ''})
        self.environment.start(); self.addCleanup(self.environment.stop)

    def test_lost_response_receipt_and_explicit_same_key_retry_select_one_phone(self):
        self.service.lose_reply = True
        with self.assertRaises(cloud.CreateError) as error:
            cloud.CloudPhone(provider='fixture')
        key = error.exception.request_key
        self.assertIsNotNone(key)
        self.assertEqual(len(self.service.posts()), 1)
        receipt = cloud.creation_receipt(key)
        self.assertEqual(len(self.service.posts()), 1)
        phone = cloud.CloudPhone(provider='fixture', request_key=key)
        self.assertEqual(phone.session_id, receipt['id'])
        self.assertEqual([r[3] for r in self.service.posts()], [key, key])
        self.assertEqual(len(self.service.sessions), 1)
        phone.release()
        with self.assertRaises(cloud.CreateError) as ended:
            cloud.CloudPhone(provider='fixture', request_key=key)
        self.assertEqual(ended.exception.status, 410)
        self.assertEqual(len(self.service.sessions), 1)

    def test_changed_body_is_rejected_and_foreign_owner_cannot_read_receipt(self):
        phone = cloud.CloudPhone(provider='fixture', request_key=REQUEST)
        with self.assertRaises(cloud.CreateError) as conflict:
            cloud.CloudPhone(provider='different', request_key=REQUEST)
        self.assertEqual(conflict.exception.status, 409)
        with self.assertRaises(cloud.RequestError) as foreign:
            cloud.creation_receipt(REQUEST, token='other-owner')
        self.assertEqual(foreign.exception.status, 404)
        self.assertEqual(cloud.creation_receipt(REQUEST)['id'], phone.session_id)
        self.assertEqual(len(self.service.sessions), 1)

    def test_unknown_receipt_never_posts_and_legacy_explicit_key_fails_before_create(self):
        with self.assertRaises(cloud.RequestError): cloud.creation_receipt(REQUEST)
        self.service.supported = False
        with self.assertRaisesRegex(RuntimeError, 'does not support'): cloud.CloudPhone(request_key=REQUEST)
        with self.assertRaisesRegex(RuntimeError, 'does not support'): cloud.creation_receipt(REQUEST)
        self.assertEqual(self.service.posts(), [])
        # Existing services remain usable with explicitly reported legacy behavior.
        phone = cloud.CloudPhone()
        self.assertIsNone(phone.request_key)
        self.assertIsNone(self.service.posts()[0][3])

    def test_invalid_keys_or_conflicting_attachment_do_not_contact_service(self):
        for key in ('short', 'a'*129, 'invalid/key-12345', 'bad\nrequest-key-1234'):
            with self.assertRaises(ValueError): cloud.CloudPhone(request_key=key)
            with self.assertRaises(ValueError): cloud.creation_receipt(key)
        with self.assertRaises(ValueError): cloud.CloudPhone(session_id='existing', request_key=REQUEST)
        self.assertEqual(self.service.requests, [])

    def test_unverified_echo_keeps_key_and_does_not_attach_or_retry(self):
        self.service.bad_echo = True
        with self.assertRaises(cloud.CreateError) as error: cloud.CloudPhone(request_key=REQUEST)
        self.assertEqual(error.exception.request_key, REQUEST)
        self.assertIsNone(error.exception.session_id)
        self.assertEqual(len(self.service.posts()), 1)
        self.assertEqual([r[1] for r in self.service.requests], ['/me', '/sessions'])

    def test_cli_prints_generated_identity_before_a_lost_response_and_can_inspect_it(self):
        self.service.lose_reply = True
        result = subprocess.run([sys.executable, '-m', 'phone_harness.run', 'cloud', 'up', 'fixture'],
                                env={**os.environ, 'PYTHONPATH': str(ROOT/'src')},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('creation request: ', result.stdout, result.stderr)
        key = result.stdout.strip().split('creation request: ')[1]
        self.assertIn(key, result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        output = io.StringIO()
        with redirect_stdout(output): self.assertEqual(cloud.cli(['receipt', key]), 0)
        self.assertEqual(json.loads(output.getvalue())['request_key'], key)
        self.assertEqual(len(self.service.posts()), 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.cli(['up', 'fixture', '--request-key', key]), 0)
        self.assertEqual(len(self.service.sessions), 1)


if __name__ == '__main__': unittest.main(verbosity=2)
