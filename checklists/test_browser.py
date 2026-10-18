from urllib.parse import urlencode, urlparse

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from .models import DiscrepancyExplanation
from .test_phase5 import Phase5FixtureMixin


class NameConfirmationBrowserTests(Phase5FixtureMixin, StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,1000")
        cls.browser = webdriver.Chrome(options=options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.quit()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.browser.delete_all_cookies()
        self.browser.get(f"{self.live_server_url}{reverse('login')}")
        self.browser.find_element(By.NAME, "username").send_keys("JSmith")
        self.browser.find_element(By.NAME, "password").send_keys(
            "phase-five-password"
        )
        self.browser.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        WebDriverWait(self.browser, 5).until(
            lambda driver: urlparse(driver.current_url).path == reverse("dashboard")
        )

    def open_checklist(self):
        query = urlencode(
            {"date": self.operational_date.isoformat(), "staff": self.staff_a.pk}
        )
        self.browser.get(
            f"{self.live_server_url}"
            f"{reverse('checklist-detail', args=(self.assignment.pk,))}?{query}"
        )
        return self.browser.find_element(By.NAME, "name_confirmation")

    def assert_custom_error(self, input_element, expected):
        self.assertEqual(
            self.browser.execute_script(
                "return arguments[0].validity.customError;", input_element
            ),
            expected,
        )

    def test_previously_invalid_name_can_be_corrected_without_reload(self):
        name_input = self.open_checklist()
        comments = self.browser.find_element(By.NAME, "explanation")
        submit = self.browser.find_element(
            By.CSS_SELECTOR, "[data-confirmation-form] button[type='submit']"
        )
        checklist_url = self.browser.current_url

        comments.send_keys("Corrected in the same browser page")
        name_input.send_keys("Wrong Name")
        submit.click()
        self.assertEqual(self.browser.current_url, checklist_url)
        self.assert_custom_error(name_input, True)
        self.assertEqual(
            name_input.get_property("validationMessage"),
            "Enter Your Name Here must match the selected staff member's complete name.",
        )

        submit.click()
        self.assertEqual(self.browser.current_url, checklist_url)
        self.assert_custom_error(name_input, True)

        name_input.clear()
        name_input.send_keys("  aLiCe WoRkEr  ")
        self.assert_custom_error(name_input, False)
        submit.click()
        WebDriverWait(self.browser, 5).until(
            lambda driver: urlparse(driver.current_url).path == reverse("dashboard")
        )
        self.assertEqual(
            DiscrepancyExplanation.objects.get(staff=self.staff_a).explanation,
            "Corrected in the same browser page",
        )

    def test_correct_then_incorrect_and_blank_names_remain_blocked(self):
        name_input = self.open_checklist()
        submit = self.browser.find_element(
            By.CSS_SELECTOR, "[data-confirmation-form] button[type='submit']"
        )
        checklist_url = self.browser.current_url

        name_input.send_keys(self.staff_a.display_name)
        self.assert_custom_error(name_input, False)
        name_input.clear()
        name_input.send_keys("Wrong Name")
        submit.click()
        self.assertEqual(self.browser.current_url, checklist_url)
        self.assert_custom_error(name_input, True)

        name_input.send_keys(Keys.CONTROL, "a")
        name_input.send_keys(Keys.BACKSPACE)
        self.assert_custom_error(name_input, False)
        self.assertTrue(
            self.browser.execute_script(
                "return arguments[0].validity.valueMissing;", name_input
            )
        )
        submit.click()
        self.assertEqual(self.browser.current_url, checklist_url)

        name_input.send_keys(self.staff_a.display_name)
        submit.click()
        WebDriverWait(self.browser, 5).until(
            lambda driver: urlparse(driver.current_url).path == reverse("dashboard")
        )
        self.assertFalse(DiscrepancyExplanation.objects.exists())
