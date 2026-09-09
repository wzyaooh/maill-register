import time
import random
import logging
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from config.settings import Config

logger = logging.getLogger('gmail_creator_appium')

class AppiumManager:
    """Manages the Android emulator to create accounts via native OS settings (No Phone Verification bypass)"""
    
    def __init__(self):
        self.driver = None
        self.wait = None

    def initialize(self, proxy=None):
        logger.info("Initializing Appium Mobile Driver...")
        
        # Configure capabilities for Nox, MEmu, or standard AVD
        options = UiAutomator2Options()
        options.platform_name = 'Android'
        options.automation_name = 'UiAutomator2'
        options.no_reset = True
        options.auto_grant_permissions = True
        
        # Connect to local Appium Server
        try:
            self.driver = webdriver.Remote('http://127.0.0.1:4723', options=options)
            self.wait = WebDriverWait(self.driver, 20)
            logger.info("Appium driver connected to Android device successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Appium server: {e}")
            logger.error("Please ensure Appium server and an Android Emulator are running.")
            return False

    def close(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

    # ======= ANDROID NATIVE AUTOMATION ======= #

    def _type_text(self, element, text):
        """Human-like typing for Android keyboards"""
        element.click()
        time.sleep(1)
        # Using execute_script to type char by char natural typing if possible
        # Appium send_keys is usually fine since it simulates keyboard input
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.1, 0.4))

    def navigate_to_add_account(self):
        """Navigates from Android home screen to Settings -> Add Account -> Google"""
        try:
            # Open Android Settings
            # Different emulators have different package names, assuming standard Android
            self.driver.activate_app("com.android.settings")
            time.sleep(2)
            
            # Scroll to find "Accounts" or "Users & accounts"
            self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 
                'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView(new UiSelector().textMatches("(?i).*Accounts.*|.*الحسابات.*"))'
            ).click()
            
            # Click "Add account"
            self.wait.until(EC.element_to_be_clickable((AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("(?i).*Add account|إضافة حساب.*")'))).click()
            
            # Click "Google"
            self.wait.until(EC.element_to_be_clickable((AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("(?i).*Google|جوجل.*")'))).click()
            
            logger.info("Successfully navigated to Google Account Setup.")
            time.sleep(10) # Wait for 'Checking info...' screen to pass
            return True
        except Exception as e:
            logger.error(f"Failed to navigate to Add Account in Android Settings: {e}")
            return False

    def start_creation_flow(self):
        """Clicks 'Create account' -> 'For myself' in the Google Setup App"""
        try:
            # Click Create Account
            create_btn = self.wait.until(EC.element_to_be_clickable(
                (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("(?i).*Create account|إنشاء حساب.*")')
            ))
            create_btn.click()
            time.sleep(2)
            
            # Click For myself or Personal use
            myself_btn = self.wait.until(EC.element_to_be_clickable(
                (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("(?i).*For myself|استخدام شخصي|لنفسي.*")')
            ))
            myself_btn.click()
            return True
        except Exception as e:
            logger.error(f"Failed to start creation flow: {e}")
            return False

    def fill_name(self, first_name, last_name):
        try:
            first_input = self.wait.until(EC.presence_of_element_located(
                (AppiumBy.CLASS_NAME, "android.widget.EditText") # The first input is usually First Name
            ))
            self._type_text(first_input, first_name)
            time.sleep(1)
            
            # Find the second EditText for last name by searching for the class again 
            # and picking index 1.
            inputs = self.driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.EditText")
            if len(inputs) > 1:
                self._type_text(inputs[1], last_name)
            
            # Click Next
            self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("(?i)Next|التالي")').click()
            return True
        except Exception as e:
            logger.error(f"Failed to fill name: {e}")
            return False
            
    def fill_birthday_gender(self, month, day, year, gender):
        # Implementation of parsing and selecting on Android native drop downs
        # Very specific to the android version UI
        try:
            time.sleep(3)
            # Day
            day_field = self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().resourceIdMatches(".*day.*")')
            self._type_text(day_field, str(day))
            
            # Year
            year_field = self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().resourceIdMatches(".*year.*")')
            self._type_text(year_field, str(year))
            
            # Month (Dropdown)
            month_spinner = self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().resourceIdMatches(".*month.*")')
            month_spinner.click()
            time.sleep(1)
            self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().textContains("{month}")').click()
            
            # Gender (Dropdown)
            gender_mapping = {"1": "Male", "2": "Female"}
            gender_text = gender_mapping.get(str(gender), "Custom")
            
            gender_spinner = self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().resourceIdMatches(".*gender.*")')
            gender_spinner.click()
            time.sleep(1)
            self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{gender_text}")').click()
            
            # Next
            self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("(?i)Next|التالي")').click()
            return True
        except Exception as e:
            logger.error(f"Failed to fill birthday/gender appium flow: {e}")
            return False

    def bypass_phone_challenge(self):
        """Looks for the 'Skip' button on the Phone Number screen if it appears."""
        try:
            # Wait a few seconds to see if phone is asked
            time.sleep(5)
            # If "Skip" is on screen, golden bypass
            skip_btn = self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("(?i)Skip|تخطي")')
            skip_btn.click()
            logger.info("APP-LEVEL PHONE BYPASS SUCCESSFUL (GOLDEN METHOD)!")
            return True
        except:
            # If no skip button, check if it's asking for phone
            try:
                self.driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("(?i).*phone|رقم الهاتف.*")')
                logger.error("Phone verification is strictly required! We have been caught.")
                return False
            except:
                # Neither skip nor phone prompt found, might be on the next screen already
                return True
