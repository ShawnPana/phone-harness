"""Run with the candidate wheel installed and PHONE_CLOUD_SOURCE set.

The API and Postgres are real local services; Android allocation is simulated.
The baseline source file is supplied explicitly as the sole command argument.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

from phone_harness import cloud
assert '/site-packages/' in cloud.__file__, 'run against an installed wheel'
root = Path(os.environ['PHONE_CLOUD_SOURCE']).resolve()
sys.path[:0] = [str(root/'tests'), str(root/'src')]
os.environ.pop('PHONE_CLOUD_SESSION', None)
import harness
import mock_shlut

spec = importlib.util.spec_from_file_location('phone_harness._baseline_cloud', sys.argv[1])
before = importlib.util.module_from_spec(spec)
spec.loader.exec_module(before)


class LostReply:
    def __init__(self, base):
        target = urlsplit(base)
        self.posts = 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def call(self):
                data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                connection = http.client.HTTPConnection(target.hostname, target.port, timeout=20)
                try:
                    connection.request(self.command, self.path, body=data,
                                       headers={k:v for k,v in self.headers.items() if k.lower() not in ('host', 'connection')})
                    reply = connection.getresponse()
                    raw, status = reply.read(), reply.status
                finally: connection.close()
                if self.command == 'POST' and self.path == '/sessions':
                    owner.posts += 1
                    if owner.posts == 1:
                        assert status == 202, status
                        self.close_connection = True
                        return
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            do_GET = do_POST = do_DELETE = call
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)


node = mock_shlut.Node(max_phones=6).start()
try:
    with harness.Service(env={
        'PHONE_CLOUD_ENV':'development', 'PHONE_CLOUD_BILLING_MODE':'disabled',
        'PHONE_CLOUD_ACCOUNT_SESSION_LIMIT':'6', 'PHONE_CLOUD_SHLUT_TARGET':'',
        'PHONE_CLOUD_SHLUT_URL':node.url, 'PHONE_CLOUD_SHLUT_TOKEN':node.token,
        'PHONE_CLOUD_TICK':'3600', 'PHONE_CLOUD_IDLE':'300'}) as service:
        results = []
        for label, sdk in (('before', before), ('after', cloud)):
            uid, client = service.renter(uuid.uuid4().hex+'@sdk.invalid', cents=0)
            proxy = LostReply(service.base)
            reserved_before = sum(m == 'POST' and p == '/phones' for m,p,_ in node.journal)
            try:
                key = None
                try:
                    sdk.CloudPhone(base=proxy.base, token=client.bearer, provider='shlut')
                except (RuntimeError, OSError) as error:
                    key = getattr(error, 'request_key', None)
                else: raise AssertionError('lost reply unexpectedly succeeded')
                assert proxy.posts == 1, 'automatic POST retry'
                options = {}
                if label == 'after':
                    assert key
                    receipt = cloud.creation_receipt(key, base=proxy.base, token=client.bearer)
                    assert proxy.posts == 1
                    options['request_key'] = key
                phone = sdk.CloudPhone(base=proxy.base, token=client.bearer, provider='shlut', **options)
                sessions = client.get('/sessions')[1]
                for session in sessions: client.wait_ready(session['id'])
                reservations = sum(m == 'POST' and p == '/phones' for m,p,_ in node.journal) - reserved_before
                if label == 'after': assert receipt['id'] == phone.session_id
                expected = 2 if label == 'before' else 1
                assert len(sessions) == reservations == expected
                results.append(dict(case=label, create_requests=proxy.posts, distinct_sessions=len(sessions), provider_reservations=reservations, real_phones=0))
                print(f'MEASURED case={label} create_requests={proxy.posts} distinct_sessions={len(sessions)} provider_reservations={reservations} real_phones=0', flush=True)
            finally:
                for session in client.get('/sessions')[1]: client.delete('/sessions/' + session['id'])
                deadline = time.monotonic() + 10
                while client.get('/sessions')[1] and time.monotonic() < deadline: time.sleep(.05)
                assert client.get('/sessions')[1] == []
                proxy.close()
        assert node.phones == {}
        print(json.dumps({'results':results, 'owned_simulated_phones_after_cleanup':0, 'installed_sdk':cloud.__file__, 'pass':True}))
finally: node.stop()
