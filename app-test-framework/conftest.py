import hashlib
import logging
from pathlib import Path
from datetime import datetime
from uuid import uuid4

import pytest

from framework.config import load_config

ROOT = Path(__file__).parent
log = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption("--artifact-dir", default=None, help="Directory for this run's evidence")
    parser.addoption("--device", action="store_true", help="Enable real Android tests")
    parser.addoption("--app-config", default=str(ROOT / "config/android.yaml"))


def pytest_configure(config):
    run = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    selected = config.getoption("--artifact-dir")
    config.artifact_dir = Path(selected).resolve() if selected else ROOT / "reports" / run
    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    if not config.option.htmlpath:
        config.option.htmlpath = str(config.artifact_dir / "report.html")
    config.option.self_contained_html = True
    config.option.log_file = str(config.artifact_dir / "run.log")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--device"):
        for item in items:
            if "device" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Enable with --device"))


@pytest.fixture
def app_config(pytestconfig):
    return load_config(pytestconfig.getoption("--app-config"))


@pytest.fixture
def driver(app_config):
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
    session = webdriver.Remote(app_config["server_url"],
        options=UiAutomator2Options().load_capabilities(app_config["capabilities"]))
    session.implicitly_wait(0)
    try:
        yield session
    finally:
        session.quit()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    session = item.funcargs.get("driver")
    if not report.failed or session is None:
        return
    stem = hashlib.sha256(item.nodeid.encode()).hexdigest()[:16] + "_" + report.when
    folder = item.config.artifact_dir
    for suffix, capture in [(".png", lambda p: session.save_screenshot(str(p))),
                            (".xml", lambda p: p.write_text(session.page_source, encoding="utf-8"))]:
        try:
            capture(folder / (stem + suffix))
        except Exception:
            log.exception("Failed to capture %s evidence", suffix)
    report.sections.append(("Evidence", str(folder / stem)))
