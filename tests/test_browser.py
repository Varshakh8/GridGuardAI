
from playwright.sync_api import sync_playwright


BASE_URL = "http://127.0.0.1:5000"


def test_billing_form_in_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(BASE_URL + "/login")
        page.locator('input[name="username"]').fill("admin")
        page.locator('input[name="password"]').fill("GridGuard@123")
        page.get_by_role("button", name="Login").click()
        page.goto(BASE_URL + "/admin")

        page.locator('input[name="consumer_name"]').fill("Test Consumer")
        page.locator('input[name="previous_reading"]').fill("100")
        page.locator('input[name="current_reading"]').fill("150")

        page.get_by_role(
            "button", name="Calculate Electricity Bill"
        ).click()

        page.get_by_text("Units Consumed: 50").wait_for()
        page.get_by_text("Total: Rs. 150.00").wait_for()

        assert "Total: Rs. 150.00" in page.locator("body").inner_text()

        browser.close()


def test_invalid_readings_in_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(BASE_URL + "/login")
        page.locator('input[name="username"]').fill("admin")
        page.locator('input[name="password"]').fill("GridGuard@123")
        page.get_by_role("button", name="Login").click()
        page.goto(BASE_URL + "/admin")

        page.locator('input[name="consumer_name"]').fill("Test Consumer")
        page.locator('input[name="previous_reading"]').fill("200")
        page.locator('input[name="current_reading"]').fill("150")

        page.get_by_role(
            "button", name="Calculate Electricity Bill"
        ).click()

        page.get_by_text(
            "Current reading cannot be less than previous reading."
        ).wait_for()

        assert "Current reading cannot be less than previous reading." in (
            page.locator("body").inner_text()
        )

        browser.close()
