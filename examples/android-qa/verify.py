#!/usr/bin/env python3
"""Install and exercise the QA fixture in one explicitly selected cloud session."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from phone_harness.cloud import CloudPhone, _request, _service

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--session', required=True, help='An existing session owned by your account')
parser.add_argument('--apk', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True, help='New evidence directory')
parser.add_argument('--release', action='store_true', help='End this exact session after the checks, including on failure')
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.session):
    parser.error('--session must be an exact session ID, not a URL or path')
args.output.mkdir(parents=True, exist_ok=False)
base, token = _service()
report = {'session_id': args.session, 'started_unix_s': time.time(), 'events': [],
          'screenshots': [], 'checks_passed': False, 'cleanup_verified': False,
          'scope': 'APK install and API-driven app QA; browser visibility and startup are separate checks'}
phone = None

def measured(name, action):
    event = {'name': name, 'started_unix_s': time.time()}
    start = time.monotonic()
    report['events'].append(event)
    try:
        result = action()
        event['status'] = 'passed'
        return result
    except Exception as error:
        event.update(status='failed', error_type=type(error).__name__, message=str(error)[:400])
        raise
    finally:
        event['elapsed_s'] = time.monotonic() - start

def expect(text):
    # Poll the app state; request acknowledgment alone is not success.
    deadline = time.monotonic() + 30
    while True:
        if any(node['text'] == text for node in phone.send('screen.text')):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f'Expected visible text: {text}')
        time.sleep(.2)

def tap(text):
    nodes = [node for node in phone.send('screen.text') if node['text'] == text and node['w'] > 0 and node['h'] > 0]
    if len(nodes) != 1:
        raise AssertionError(f'Expected one tap target for {text}, found {len(nodes)}')
    node = nodes[0]
    # screen.text already supplies device-pixel centers, not top-left bounds.
    phone.send('input.tap', x=node['x'], y=node['y'])

def screenshot(name):
    path = args.output / (name + '.png')
    phone.send('screen.capture', path=str(path))
    report['screenshots'].append({'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})

try:
    phone = measured('attach_existing_session', lambda: CloudPhone(base=base, token=token, session_id=args.session))
    report['install_receipt'] = measured('upload_and_install', lambda: phone.install_apk(args.apk))
    measured('launch', lambda: phone.send('apps.launch', name='com.phoneharness.qa'))
    if measured('verify_foreground_package', lambda: phone.send('apps.current')) != 'com.phoneharness.qa':
        raise AssertionError('QA fixture is not the foreground package')
    measured('initial_counter', lambda: expect('Count: 0'))
    screenshot('01-installed')
    measured('increment_input', lambda: tap('Increment'))
    measured('increment_observed', lambda: expect('Count: 1'))
    screenshot('02-increment')
    measured('reset_input', lambda: tap('Reset'))
    measured('reset_observed', lambda: expect('Count: 0'))
    measured('empty_submit_input', lambda: tap('Submit'))
    measured('required_name_observed', lambda: expect('Name is required'))
    screenshot('03-validation-error')
    nodes = phone.send('screen.text')
    label = 'Enter a name' if any(n['text'] == 'Enter a name' for n in nodes) else 'Name input'
    measured('name_focus_input', lambda: tap(label))
    measured('name_text_input', lambda: phone.send('input.text', s='Ada'))
    measured('hide_keyboard', lambda: phone.send('nav.back'))
    measured('submit_input', lambda: tap('Submit'))
    measured('greeting_observed', lambda: expect('Hello, Ada!'))
    screenshot('04-greeting')
    report['checks_passed'] = True
except Exception as error:
    report['failure'] = {'type': type(error).__name__, 'message': str(error)[:400]}
    if phone is not None:
        try:
            screenshot('failure')
        except Exception as capture_error:
            report['failure_capture_error'] = type(capture_error).__name__
finally:
    if args.release:
        try:
            # --release applies to the explicit ID even if attachment failed.
            # Account authorization still gates both cleanup requests.
            report['release_receipt'] = measured('release', lambda:
                _request(base, token, 'DELETE', '/sessions/' + args.session))
            def wait_cleanup():
                deadline = time.monotonic() + 120
                while True:
                    sessions = _request(base, token, 'GET', '/sessions')
                    if not any(s['id'] == args.session for s in sessions):
                        return True
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Session cleanup still pending')
                    time.sleep(1)
            report['cleanup_verified'] = measured('verify_active_session_removed', wait_cleanup)
        except Exception as error:
            report['cleanup_error'] = {'type': type(error).__name__, 'message': str(error)[:400]}
    report['ended_unix_s'] = time.time()
    (args.output/'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str((args.output/'report.json').resolve()),
                      'checks_passed': report['checks_passed'], 'cleanup_verified': report['cleanup_verified']}))

raise SystemExit(0 if report['checks_passed'] and (not args.release or report['cleanup_verified']) else 1)
