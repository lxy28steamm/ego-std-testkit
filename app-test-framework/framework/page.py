import logging

from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

log = logging.getLogger(__name__)


class BasePage:
    def __init__(self, driver, timeout=15):
        self.driver = driver
        self.wait = WebDriverWait(driver, timeout)

    def visible(self, locator):
        return self.wait.until(EC.visibility_of_element_located(locator))

    def click(self, locator):
        log.info("Click element: %s", locator)
        self.wait.until(EC.element_to_be_clickable(locator)).click()

    def fill(self, locator, value):
        # Do not log input values: they may contain credentials.
        log.info("Fill element: %s", locator)
        element = self.visible(locator)
        element.clear()
        element.send_keys(value)

    def text(self, locator):
        return self.visible(locator).text
