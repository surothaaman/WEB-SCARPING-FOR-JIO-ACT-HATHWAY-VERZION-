from pathlib import Path

from playwright.sync_api import sync_playwright, expect

URL = "https://www.jio.com/selfcare/plans/fiber/fiber-postpaid-plans-list/"
OUTPUT_DIR = Path(__file__).resolve().parent / "jio_tab_captures"
OUTPUT_DIR.mkdir(exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)

    try:
        page = browser.new_page()
        page.set_default_timeout(15000)

        page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        tabs = page.get_by_role("tab")
        tabs.first.wait_for(state="visible")

        tab_names = [
            name.strip()
            for name in tabs.all_inner_texts()
            if name.strip()
        ]

        print("Tabs found:", tab_names)

        for index, name in enumerate(tab_names, start=1):
            print(f"\nInspecting: {name}")

            try:
                tab = page.get_by_role("tab", name=name, exact=True)
                tab.click()

                expect(tab).to_have_attribute(
                    "aria-selected",
                    "true",
                )

                # Temporary inspection delay.
                # Tab selection alone does not confirm plans have loaded.
                page.wait_for_timeout(2500)

                filename = f"{index:02d}_{name.lower().replace(' ', '_')}"

                (OUTPUT_DIR / f"{filename}.html").write_text(
                    page.content(),
                    encoding="utf-8",
                )

                (OUTPUT_DIR / f"{filename}.txt").write_text(
                    page.locator("body").inner_text(),
                    encoding="utf-8",
                )

                print(f"Saved: {filename}.html and {filename}.txt")

            except Exception as error:
                print(f"Failed to inspect {name}: {error}")

        print(f"\nCaptures saved in: {OUTPUT_DIR}")

    finally:
        browser.close()