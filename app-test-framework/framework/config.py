import os
from pathlib import Path

import yaml


def load_config(path):
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a YAML mapping")
    caps = data.get("capabilities")
    if not isinstance(caps, dict):
        raise ValueError("capabilities must be a mapping")
    if caps.get("platformName") != "Android":
        raise ValueError("This framework currently supports Android only")
    for env, key in [("ANDROID_UDID", "appium:udid"),
                     ("APP_PACKAGE", "appium:appPackage"),
                     ("APP_ACTIVITY", "appium:appActivity"),
                     ("APP_APK", "appium:app")]:
        if os.environ.get(env):
            caps[key] = os.environ[env]
    if not caps.get("appium:udid"):
        raise ValueError("Set ANDROID_UDID explicitly; automatic device selection is disabled")
    if not caps.get("appium:app") and not all(caps.get(k) for k in
            ("appium:appPackage", "appium:appActivity")):
        raise ValueError("Set an APK or both appPackage and appActivity")
    data["server_url"] = os.environ.get("APPIUM_URL", data.get("server_url", "http://127.0.0.1:4723"))
    data["wait_seconds"] = float(data.get("wait_seconds", 15))
    if data["wait_seconds"] <= 0:
        raise ValueError("wait_seconds must be positive")
    return data
