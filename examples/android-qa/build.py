#!/usr/bin/env python3
"""Build a signed, disposable QA APK using an already installed Android SDK."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--sdk', type=Path, required=True)
parser.add_argument('--java', type=Path, required=True, help='JDK directory containing bin/java and bin/javac')
parser.add_argument('--build-tools', default='35.0.0')
parser.add_argument('--platform', default='android-35')
parser.add_argument('--output', type=Path, required=True, help='New APK path; existing files are not overwritten')
args = parser.parse_args()
source = Path(__file__).resolve().parent
tools = args.sdk.resolve() / 'build-tools' / args.build_tools
platform = args.sdk.resolve() / 'platforms' / args.platform / 'android.jar'
java = args.java.resolve() / 'bin'
output = args.output.resolve()
if output.exists():
    parser.error('output already exists')
for path in [tools/'aapt2', tools/'zipalign', tools/'lib/d8.jar', tools/'lib/apksigner.jar', platform, java/'java', java/'javac', java/'keytool']:
    if not path.is_file():
        parser.error(f'missing tool: {path}')
output.parent.mkdir(parents=True, exist_ok=True)

def run(*argv):
    return subprocess.check_output([str(x) for x in argv], stderr=subprocess.STDOUT, text=True)

with tempfile.TemporaryDirectory(prefix='phone-harness-qa-build-') as temporary:
    work = Path(temporary)
    classes, dex = work/'classes', work/'dex'
    classes.mkdir()
    dex.mkdir()
    run(java/'javac', '--release', '8', '-classpath', platform, '-d', classes, source/'MainActivity.java')
    run(java/'java', '-cp', tools/'lib/d8.jar', 'com.android.tools.r8.D8', '--min-api', '23',
        '--lib', platform, '--output', dex, *sorted(classes.rglob('*.class')))
    run(tools/'aapt2', 'link', '--manifest', source/'AndroidManifest.xml', '-I', platform, '-o', work/'unsigned.apk')
    with zipfile.ZipFile(work/'unsigned.apk', 'a', compression=zipfile.ZIP_DEFLATED) as apk:
        apk.write(dex/'classes.dex', 'classes.dex')
    run(tools/'zipalign', '-p', '4', work/'unsigned.apk', work/'aligned.apk')
    run(java/'keytool', '-genkeypair', '-keystore', work/'debug.jks', '-storepass', 'android',
        '-keypass', 'android', '-alias', 'androiddebugkey', '-keyalg', 'RSA', '-keysize', '2048',
        '-validity', '30', '-dname', 'CN=Phone Harness QA Test, O=Development, C=US')
    run(java/'java', '-jar', tools/'lib/apksigner.jar', 'sign', '--ks', work/'debug.jks',
        '--ks-pass', 'pass:android', '--key-pass', 'pass:android', '--out', work/'signed.apk', work/'aligned.apk')
    verification = run(java/'java', '-jar', tools/'lib/apksigner.jar', 'verify', '--verbose', '--print-certs', work/'signed.apk')
    badging = run(tools/'aapt2', 'dump', 'badging', work/'signed.apk')
    if "package: name='com.phoneharness.qa'" not in badging or "launchable-activity: name='com.phoneharness.qa.MainActivity'" not in badging:
        raise RuntimeError('APK package or launcher does not match the test fixture')
    data = (work/'signed.apk').read_bytes()
    with output.open('xb') as target:
        target.write(data)
    receipt = {
        'apk': str(output), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
        'package': 'com.phoneharness.qa', 'build_tools': args.build_tools, 'platform': args.platform,
        'source_sha256': {name: hashlib.sha256((source/name).read_bytes()).hexdigest()
                          for name in ['MainActivity.java', 'AndroidManifest.xml', 'build.py']},
        'java_version': run(java/'java', '-version'), 'signature_verification': verification,
        'badging': badging, 'real_installs': 0,
    }
    output.with_suffix('.build.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'apk': str(output), 'bytes': len(data), 'sha256': receipt['sha256'], 'signature_verified': True, 'real_installs': 0}))
