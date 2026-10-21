import pytest
import yaml

from framework.config import load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("ANDROID_UDID", "APP_PACKAGE", "APP_ACTIVITY", "APP_APK", "APPIUM_URL"):
        monkeypatch.delenv(name, raising=False)


def config_file(tmp_path, caps):
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump({"capabilities": caps}), encoding="utf-8")
    return path


def test_reject_implicit_device_selection(tmp_path):
    with pytest.raises(ValueError, match="ANDROID_UDID"):
        load_config(config_file(tmp_path, {"platformName": "Android"}))


def test_environment_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_UDID", "test-serial")
    monkeypatch.setenv("APP_PACKAGE", "com.example.app")
    monkeypatch.setenv("APP_ACTIVITY", ".MainActivity")
    monkeypatch.setenv("APPIUM_URL", "http://localhost:4725")
    config = load_config(config_file(tmp_path, {"platformName": "Android"}))
    assert config["capabilities"]["appium:udid"] == "test-serial"
    assert config["capabilities"]["appium:appPackage"] == "com.example.app"
    assert config["server_url"] == "http://localhost:4725"


def test_missing_app_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_UDID", "test-serial")
    with pytest.raises(ValueError, match="APK"):
        load_config(config_file(tmp_path, {"platformName": "Android"}))
