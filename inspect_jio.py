import json
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://www.jio.com/selfcare/plans/fiber/fiber-postpaid-plans-list/"
OUTPUT_DIR = Path(__file__).resolve().parent

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=False)

    try:
        page = browser.new_page()

        response = page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        print("HTTP status:", response.status if response else "Unknown")

        input(
            "Wait until the plans appear in the browser. "
            "Then return here and press Enter..."
        )

        # Save the current rendered HTML.
        (OUTPUT_DIR / "jio_page.html").write_text(
            page.content(),
            encoding="utf-8",
        )

        # Save readable page text.
        (OUTPUT_DIR / "jio_page_text.txt").write_text(
            page.locator("body").inner_text(),
            encoding="utf-8",
        )

        # Inspect visible buttons, tabs, and dropdown controls.
        controls = page.locator(
            "button, [role='tab'], select, [role='combobox']"
        ).evaluate_all("""
            elements => elements
                .filter(element => element.getClientRects().length > 0)
                .map(element => ({
                    tag: element.tagName,
                    role: element.getAttribute("role"),
                    text: (element.innerText || "").trim(),
                    aria_label: element.getAttribute("aria-label"),
                    aria_selected: element.getAttribute("aria-selected"),
                    aria_expanded: element.getAttribute("aria-expanded"),
                    class_name: element.getAttribute("class"),
                    html: element.outerHTML
                }))
        """)

        (OUTPUT_DIR / "jio_controls.json").write_text(
            json.dumps(
                controls,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print("Page title:", page.title())
        print("Final URL:", page.url)
        print("Visible controls found:", len(controls))
        print("\\nSaved files:")
        print("1. jio_page.html")
        print("2. jio_page_text.txt")
        print("3. jio_controls.json")

    finally:
        browser.close()