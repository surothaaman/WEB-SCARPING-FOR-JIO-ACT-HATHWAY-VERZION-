import os
from pathlib import Path
from time import perf_counter

from browserbase import Browserbase
from dotenv import load_dotenv
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

URL = "https://hathwaybroadband.com/internet-service-provider/chennai/"
OUTPUT_DIR = BASE_DIR / "captures"
OUTPUT_DIR.mkdir(exist_ok=True)

api_key = os.getenv("BROWSERBASE_API_KEY")
project_id = os.getenv("BROWSERBASE_PROJECT_ID")

if not api_key or not project_id:
    raise ValueError("Add your Browserbase credentials to .env.")

client = Browserbase(api_key=api_key)
start = perf_counter()

session = client.sessions.create(
    project_id=project_id,
    timeout=180,
    keep_alive=False,
)

with sync_playwright() as playwright:
    browser = None

    try:
        browser = playwright.chromium.connect_over_cdp(
            session.connect_url,
            timeout=60000,
        )

        page = browser.contexts[0].new_page()

        response = page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print("HTTP status:", response.status if response else "Unknown")

        # Diagnostic check: wait for broadband speed text.
        # This does not prove every plan has loaded.
        try:
            page.wait_for_function(
                "() => /Mbps/i.test(document.body?.innerText || '')",
                timeout=30000,
            )
            print("Speed text detected.")
        except PlaywrightTimeoutError:
            print("No speed text detected. Saving the page for diagnosis.")

        (OUTPUT_DIR / "hathway_page.html").write_text(
            page.content(),
            encoding="utf-8",
        )

        (OUTPUT_DIR / "hathway_page_text.txt").write_text(
            page.locator("body").inner_text(),
            encoding="utf-8",
        )

        print("Page title:", page.title())
        print("Final URL:", page.url)
        print("Saved: captures/hathway_page.html")
        print("Saved: captures/hathway_page_text.txt")

    finally:
        if browser is not None:
            browser.close()

print(f"Inspection time: {perf_counter() - start:.2f} seconds")