from appium.webdriver.common.appiumby import AppiumBy

from framework.page import BasePage


class SettingsPage(BasePage):
    """Demonstration only. OEM Settings screens may use different locators."""
    SEARCH = (AppiumBy.ACCESSIBILITY_ID, "Search settings")

    def open_search(self):
        self.click(self.SEARCH)
