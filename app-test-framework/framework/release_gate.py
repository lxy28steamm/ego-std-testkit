"""Read-only release gate, executed before device fixtures can change the app."""
import re
import subprocess

PACKAGE = 'com.livsyn.livsyn_collection_app'

def verify_release(serial, command=subprocess.run):
    if not serial:
        raise ValueError('ANDROID_UDID must explicitly identify the test device')
    result = command(['adb', '-s', serial, 'shell', 'dumpsys', 'package', PACKAGE],
                     capture_output=True, text=True, check=True, timeout=30)
    name = re.search(r'\bversionName=([^\s]+)', result.stdout)
    code = re.search(r'\bversionCode=(\d+)', result.stdout)
    actual = (name[1] if name else None, int(code[1]) if code else None)
    if actual != ('2.3.0', 111):
        raise ValueError(f'Expected LivFlow 2.3.0 / 111; installed={actual}')
    return {'serial': serial, 'package': PACKAGE, 'version_name': actual[0],
            'version_code': actual[1], 'variant': 'not independently verified'}
