import subprocess
from unittest.mock import Mock
import pytest
from framework.release_gate import verify_release

def test_wrong_build_rejected():
    cmd=Mock(return_value=subprocess.CompletedProcess([],0,'versionName=2.3.0\nversionCode=105'))
    with pytest.raises(ValueError): verify_release('target',cmd)

def test_missing_device_never_calls_adb():
    cmd=Mock()
    with pytest.raises(ValueError): verify_release('',cmd)
    cmd.assert_not_called()

def test_expected_build_records_explicit_device():
    cmd=Mock(return_value=subprocess.CompletedProcess([],0,'versionName=2.3.0\nversionCode=111'))
    assert verify_release('target',cmd)['version_code']==111
    assert cmd.call_args.args[0][:3]==['adb','-s','target']
