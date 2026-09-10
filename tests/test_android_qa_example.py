"""Exercise the shipped example against HTTP app outcomes, without Android."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SID = 'qa-example-owned'
KEY = 'qa-example-account-key'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==')


class AppService:
    def __init__(self, fail_increment=False, fail_attachment=False):
        self.requests = []
        self.count = 0
        self.name = self.result = ''
        self.focused = self.launched = self.released = False
        self.fail_increment = fail_increment
        self.fail_attachment = fail_attachment
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                owner.requests.append((self.command, self.path))
                if self.headers.get('Authorization') != 'Bearer ' + KEY:
                    status, result = 401, {'error': 'account key required'}
                else:
                    status, result = owner.dispatch(self.command, self.path, body)
                payload = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    @staticmethod
    def node(text, y):
        # shlut's AdbOps.flatten and screen.text return CENTER coordinates.
        return {'text': text, 'x': 360, 'y': y, 'w': 600, 'h': 80,
                'source': 'tree', 'confidence': 1.0}

    def dispatch(self, method, path, body):
        if method == 'GET' and path == '/sessions/' + SID:
            if self.fail_attachment:
                return 503, {'error': 'fixture rejected attachment'}
            return 200, {'id': SID, 'state': 'ready', 'ops': []}
        if method == 'DELETE' and path == '/sessions/' + SID:
            self.released = True
            return 202, {'cleanup_pending': True, 'state': 'closing'}
        if method == 'GET' and path == '/sessions':
            return 200, [] if self.released else [{'id': SID}]
        if method == 'POST' and path == f'/sessions/{SID}/apk':
            return 200, {'installed': True, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        if method != 'POST' or path != f'/sessions/{SID}/op':
            return 400, {'error': 'unexpected allocation or target'}
        data = json.loads(body)
        op, kw = data['op'], data['kw']
        if op == 'apps.launch':
            self.launched = kw['name'] == 'com.phoneharness.qa'
            result = None
        elif op == 'apps.current':
            result = 'com.phoneharness.qa' if self.launched else 'launcher'
        elif op == 'screen.text':
            result = [self.node('Count: ' + str(self.count), 200),
                      self.node('Increment', 320), self.node('Reset', 440),
                      self.node(self.name or 'Name input', 560),
                      self.node('Submit', 680), self.node(self.result, 800)]
        elif op == 'screen.capture':
            result = {'png_b64': base64.b64encode(PNG).decode(),
                      'bounds': {'x': 0, 'y': 0, 'w': 1, 'h': 1}}
        elif op == 'input.tap':
            target = next((y for y in (320, 440, 560, 680)
                           if 60 <= kw['x'] < 660 and y-40 <= kw['y'] < y+40), None)
            if target is None:
                return 400, {'error': 'tap missed every actionable element'}
            if target == 320:
                if self.fail_increment:
                    return 503, {'error': 'fixture rejected increment'}
                self.count += 1
            elif target == 440:
                self.count = 0
            elif target == 560:
                self.focused = True
            elif target == 680:
                self.result = 'Hello, ' + self.name + '!' if self.name else 'Name is required'
            result = None
        elif op == 'input.text' and self.focused:
            self.name = kw['s']
            result = None
        elif op == 'nav.back':
            self.focused = False
            result = None
        else:
            return 400, {'error': 'unexpected operation'}
        return 200, {'result': result}


class AndroidQaExample(unittest.TestCase):
    def run_example(self, fail_increment=False, fail_attachment=False):
        service = AppService(fail_increment, fail_attachment)
        self.addCleanup(service.close)
        temporary = tempfile.TemporaryDirectory(prefix='qa-example-contract-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        apk = root/'fixture.apk'
        apk.write_bytes(b'PK\x03\x04example HTTP fixture, not an Android APK')
        env = {**os.environ, 'PHONE_CLOUD_URL': service.base, 'PHONE_CLOUD_TOKEN': KEY,
               'PYTHONPATH': str(ROOT/'src')}
        # In installed-wheel CI ROOT has tests/examples only; there is no src.
        process = subprocess.run([sys.executable, str(ROOT/'examples/android-qa/verify.py'),
                                  '--session', SID, '--apk', str(apk),
                                  '--output', str(root/'evidence'), '--release'],
                                 env=env, capture_output=True, text=True, timeout=20)
        self.assertNotIn(KEY, process.stdout + process.stderr)
        report = json.loads((root/'evidence/report.json').read_text())
        self.assertTrue(report['cleanup_verified'], report)
        self.assertTrue(service.released)
        self.assertNotIn(('POST', '/sessions'), service.requests)
        self.assertEqual(sum(method == 'DELETE' for method, _ in service.requests), 1)
        self.assertTrue(all(path == '/sessions' or path.startswith('/sessions/' + SID)
                            for _, path in service.requests))
        for screenshot in report['screenshots']:
            image = root/'evidence'/screenshot['file']
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), screenshot['sha256'])
        return process, report, service

    def test_app_outcomes_and_center_coordinates(self):
        process, report, service = self.run_example()
        self.assertEqual(process.returncode, 0, report)
        self.assertTrue(report['checks_passed'])
        self.assertEqual(service.result, 'Hello, Ada!')
        self.assertEqual(len(report['screenshots']), 4)

    def test_failed_action_still_releases_exact_session_and_reports_failure(self):
        process, report, _ = self.run_example(fail_increment=True)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(report['checks_passed'])
        self.assertIn('fixture rejected increment', report['failure']['message'])
        self.assertEqual([e['name'] for e in report['events'] if e['status'] == 'failed'], ['increment_input'])
        self.assertEqual(report['screenshots'][-1]['file'], 'failure.png')

    def test_failed_attachment_still_releases_explicitly_selected_session(self):
        process, report, service = self.run_example(fail_attachment=True)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(report['checks_passed'])
        self.assertIn('fixture rejected attachment', report['failure']['message'])
        self.assertEqual(report['screenshots'], [])
        self.assertEqual(service.requests, [('GET', '/sessions/' + SID),
            ('DELETE', '/sessions/' + SID), ('GET', '/sessions')])

    def test_invalid_cleanup_target_is_rejected_before_network(self):
        service = AppService()
        self.addCleanup(service.close)
        with tempfile.TemporaryDirectory(prefix='qa-invalid-target-') as directory:
            env = {**os.environ, 'PHONE_CLOUD_URL': service.base, 'PHONE_CLOUD_TOKEN': KEY,
                   'PYTHONPATH': str(ROOT/'src')}
            process = subprocess.run([sys.executable, str(ROOT/'examples/android-qa/verify.py'),
                '--session', '../other?query=x', '--apk', str(Path(directory)/'unused.apk'),
                '--output', str(Path(directory)/'evidence'), '--release'],
                env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 2)
            self.assertEqual(service.requests, [])
            self.assertFalse((Path(directory)/'evidence').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
