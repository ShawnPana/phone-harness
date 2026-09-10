"""Customer client contract over actual local HTTP, with simulated sessions."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from phone_harness import cloud

BODY = b'PK\x03\x04' + b'client-fixture-only' * 5000
KEY = 'pck_client_fixture_only'
SID = 'owned-session'


class Service:
    def __init__(self):
        self.requests = []
        self.status, self.result, self.redirect = 200, None, None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def request(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(length)
                owner.requests.append((self.command, self.path, dict(self.headers), body))
                if owner.redirect:
                    self.send_response(307)
                    self.send_header('Location', owner.redirect)
                    self.end_headers()
                    return
                if self.command == 'GET':
                    result, status = {'id': SID, 'state': 'ready', 'ops': []}, 200
                elif self.command == 'DELETE':
                    result, status = {'cleanup_pending': True, 'state': 'closing'}, 202
                elif self.path == '/sessions/' + SID + '/apk':
                    result = owner.result if owner.result is not None else {
                        'installed': True, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
                    status = owner.status
                else:
                    result, status = {'error': 'unexpected request'}, 400
                payload = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            do_GET = do_POST = do_DELETE = request
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class Install(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.addCleanup(self.service.close)
        self.tmp = tempfile.TemporaryDirectory(prefix='harness-apk-test-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.apk'
        self.path.write_bytes(BODY)

    def install(self, **kwargs):
        return cloud._install_apk(self.service.base, KEY, SID, self.path, **kwargs)

    def test_exact_bytes_hash_target_and_no_provisioning(self):
        result = self.install()
        self.assertEqual(result, {'installed': True, 'bytes': len(BODY), 'sha256': hashlib.sha256(BODY).hexdigest()})
        self.assertEqual(len(self.service.requests), 1)
        method, path, headers, body = self.service.requests[0]
        self.assertEqual((method, path, body), ('POST', f'/sessions/{SID}/apk', BODY))
        self.assertEqual(headers['Authorization'], 'Bearer ' + KEY)
        self.assertEqual(headers['Content-Length'], str(len(BODY)))
        self.assertEqual(headers['X-Apk-Sha256'], result['sha256'])
        print(f'MEASURED client_apk_bytes={len(BODY)} existing_session_uploads=1 sessions_created=0 real_phones=0')

    def test_bound_phone_installs_and_preserves_pending_release_receipt(self):
        phone = cloud.CloudPhone(self.service.base, KEY, session_id=SID)
        self.assertTrue(phone.install_apk(self.path)['installed'])
        self.assertTrue(phone.release()['cleanup_pending'])
        self.assertEqual([r[:2] for r in self.service.requests], [
            ('GET', f'/sessions/{SID}'), ('POST', f'/sessions/{SID}/apk'), ('DELETE', f'/sessions/{SID}')])

    def test_receipt_mismatch_and_server_failures_do_not_retry(self):
        for status, result, outcome in [(200, {'installed': True, 'bytes': 1, 'sha256': 'wrong'}, 'unknown'),
                                        (422, {'error': 'Bad APK', 'code': 'invalid_apk'}, None),
                                        (502, {'error': 'Lost worker receipt', 'outcome': 'unknown'}, 'unknown')]:
            self.service.status, self.service.result = status, result
            before = len(self.service.requests)
            with self.assertRaises(cloud.InstallError) as caught:
                self.install()
            self.assertEqual(caught.exception.outcome, outcome)
            self.assertEqual(len(self.service.requests), before + 1)

    def test_redirects_cannot_forward_apk_or_json_credentials(self):
        destination = Service()
        self.addCleanup(destination.close)
        self.service.redirect = destination.base + '/capture'
        with self.assertRaises(cloud.InstallError):
            self.install()
        with self.assertRaises(RuntimeError):
            cloud._request(self.service.base, KEY, 'GET', '/sessions/' + SID)
        with self.assertRaises(RuntimeError):
            cloud._request(self.service.base, KEY, 'POST', '/sessions', {})
        self.assertEqual(destination.requests, [])

    def test_invalid_files_credentials_and_ids_fail_before_network(self):
        self.path.write_bytes(b'not an APK')
        cases = [(KEY, SID, self.path), (None, SID, self.path), (KEY, '../other?token=x', self.path),
                 (KEY, SID, self.path.with_suffix('.aab')), (KEY, SID, self.path.with_name('missing.apk'))]
        for token, sid, path in cases:
            with self.assertRaises(cloud.InstallError):
                cloud._install_apk(self.service.base, token, sid, path)
        with self.path.open('wb') as out:
            out.truncate(cloud.MAX_APK_BYTES + 1)
        with self.assertRaises(cloud.InstallError):
            self.install()
        directory = Path(self.tmp.name) / 'directory.apk'
        directory.mkdir()
        with self.assertRaises(cloud.InstallError):
            cloud._install_apk(self.service.base, KEY, SID, directory)
        if hasattr(os, 'mkfifo'):
            fifo = Path(self.tmp.name) / 'pipe.apk'
            os.mkfifo(fifo)
            with self.assertRaises(cloud.InstallError):
                cloud._install_apk(self.service.base, KEY, SID, fifo)
        self.assertEqual(self.service.requests, [])

    def test_body_is_bounded_when_file_grows_and_detects_truncation(self):
        body = cloud._UploadBody(io.BytesIO(b'original-appended'), len(b'original'))
        self.assertEqual(body.read(1000), b'original')
        self.assertEqual(body.read(1000), b'')
        body = cloud._UploadBody(io.BytesIO(b'short'), 100)
        self.assertEqual(body.read(100), b'short')
        with self.assertRaises(OSError):
            body.read(100)

    def test_unsafe_service_urls_and_attachment_ids_never_send_credentials(self):
        for base in ['http://example.com', 'http://localhost', 'http://192.0.2.1',
                     'https://user:secret@example.com', 'https://example.com?secret=x',
                     'https://example.com/#secret', 'https://example.com:bad',
                     'https://example.com\n', 'https://example.com\\@other.test']:
            with self.subTest(base=base), patch.object(cloud._OPENER, 'open') as network:
                with self.assertRaises(cloud.InstallError):
                    cloud._install_apk(base, KEY, SID, self.path)
                with self.assertRaises(RuntimeError):
                    cloud._request(base, KEY, 'GET', '/sessions')
                network.assert_not_called()
        for sid in ['../other', 'session?token=x', 'session/#fragment']:
            with self.assertRaisesRegex(RuntimeError, 'invalid session ID'):
                cloud.CloudPhone(self.service.base, KEY, session_id=sid)
        self.assertEqual(self.service.requests, [])
        self.assertEqual(cloud._service_url('http://[::1]:8722'), 'http://[::1]:8722')
        self.assertEqual(cloud._service_url('https://example.com/'), 'https://example.com')

    def test_actual_cli_uploads_to_existing_session_without_printing_key(self):
        env = {**os.environ, 'PYTHONPATH': str(ROOT/'src'), 'PHONE_CLOUD_URL': self.service.base,
               'PHONE_CLOUD_TOKEN': KEY}
        result = subprocess.run([sys.executable, '-m', 'phone_harness.run', 'cloud', 'install', SID, str(self.path)],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'{SID}  installed {len(BODY)} bytes', result.stdout)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertEqual(len(self.service.requests), 1)

    def test_cli_reports_uncertainty_without_retrying(self):
        self.service.status, self.service.result = 502, {'outcome': 'unknown'}
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {'PHONE_CLOUD_URL': self.service.base, 'PHONE_CLOUD_TOKEN': KEY}), redirect_stdout(out), redirect_stderr(err):
            code = cloud.cli(['install', SID, str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('inspect the phone before retrying', err.getvalue())
        self.assertEqual(len(self.service.requests), 1)

    def test_closing_session_is_not_ready_and_cli_labels_pending_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, 'not ready'):
            cloud._wait_ready(self.service.base, KEY, {'id': SID, 'state': 'closing'})
        output = io.StringIO()
        with patch.dict(os.environ, {'PHONE_CLOUD_URL': self.service.base, 'PHONE_CLOUD_TOKEN': KEY}), redirect_stdout(output):
            self.assertEqual(cloud.cli(['down', SID]), 0)
        self.assertIn('billing stopped; cleanup is still pending', output.getvalue())
        self.assertNotIn('not found', output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            cloud._banner({'id': SID, 'device': 'Android', 'provider': 'shlut'})
        self.assertIn('cloud phone ready: Android', output.getvalue())
        self.assertNotIn('iPhone', output.getvalue())

    def test_pixel_ocr_is_not_advertised_on_non_mac_clients(self):
        phone = cloud.CloudPhone(self.service.base, KEY, session_id=SID)
        with patch.object(cloud.sys, 'platform', 'linux'):
            self.assertFalse(phone.supports('screen.text_pixels'))
            with self.assertRaises(cloud.Unsupported):
                phone.send('screen.text_pixels')
        self.assertEqual(len(self.service.requests), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
