import pytest
from selenium.webdriver.support.ui import WebDriverWait


@pytest.mark.device
@pytest.mark.smoke
def test_app_launch(driver, app_config):
    expected = app_config["capabilities"].get("appium:appPackage")
    if not expected:
        pytest.fail("Launch assertion requires appPackage; configure the expected package")
    WebDriverWait(driver, app_config["wait_seconds"]).until(
        lambda d: d.current_package == expected,
        message=f"App did not reach expected foreground package: {expected}")
