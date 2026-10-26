import os
import re
import sys
from pathlib import Path
from time import perf_counter

from browserbase import Browserbase
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).resolve().parent / ".env")

URL = "https://www.jio.com/selfcare/plans/fiber/fiber-prepaid-plans-home/"
ALL_TABS = "--all-tabs" in sys.argv
BLOCK_ASSETS = "--block-assets" in sys.argv

API_KEY = os.getenv("BROWSERBASE_API_KEY")
PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID")

if not API_KEY or not PROJECT_ID:
    raise ValueError("Add your Browserbase credentials to .env.")

timings = {}
total_start = perf_counter()
plans = []
success = False

client = Browserbase(api_key=API_KEY)

try:
    start = perf_counter()

    session = client.sessions.create(
        project_id=PROJECT_ID,
        timeout=180,
        keep_alive=False,
    )

    timings["Session creation"] = perf_counter() - start

    start = perf_counter()

    with sync_playwright() as playwright:
        timings["Playwright startup"] = perf_counter() - start
        browser = None

        try:
            start = perf_counter()

            browser = playwright.chromium.connect_over_cdp(
                session.connect_url,
                timeout=60000,
            )

            timings["Browser connection"] = perf_counter() - start

            start = perf_counter()

            context = browser.contexts[0]
            page = context.new_page()

            if BLOCK_ASSETS:
                # Match asset URLs only, avoiding interception of every
                # script and API request over the cloud connection.
                asset_pattern = re.compile(
                    r"\.(?:png|jpg|jpeg|gif|webp|svg|ico|"
                    r"woff|woff2|ttf|otf|mp4|webm)(?:\?.*)?$",
                    re.IGNORECASE,
                )

                page.route(
                    asset_pattern,
                    lambda route: route.abort(),
                )

            timings["Page setup"] = perf_counter() - start

            start = perf_counter()

            response = page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            timings["Navigation to DOM ready"] = perf_counter() - start

            if response is None or not response.ok:
                raise RuntimeError(
                    "Jio did not return a successful response."
                )

            start = perf_counter()

            cards = page.locator(
                '[class*="planDetailsCard_container"]'
            )

            cards.first.locator(
                '[class*="DataCol_font"]'
            ).wait_for(state="visible", timeout=45000)

            timings["Wait for first plan"] = perf_counter() - start

            start = perf_counter()

            plans = cards.evaluate_all("""
                cards => cards.map(card => ({
                    price: card.querySelector(
                        'h3[class*="PlanName_planText"]'
                    )?.innerText.trim() ?? null,

                    validity: card.querySelector(
                        '[class*="ValidityCol_gridItem"] .j-text-body-xxs-bold'
                    )?.innerText.trim() ?? null,

                    data_and_speed: card.querySelector(
                        '[class*="DataCol_font"]'
                    )?.innerText.trim() ?? null
                }))
            """)

            required = ("price", "validity", "data_and_speed")

            if not plans or any(
                not plan[field]
                for plan in plans
                for field in required
            ):
                raise ValueError(
                    "The captured plans are empty or incomplete."
                )

            timings["Extraction and validation"] = perf_counter() - start
            timings["Elapsed until data ready"] = perf_counter() - total_start
            # Success is reported after the database save completes.
                        
            if ALL_TABS:
                # Only inspect plan duration tabs, never purchase/account buttons.
                allowed = {"wi-fi plans", "monthly", "quarterly", "semi-annual", "annual"}
                names = [name.strip() for name in page.get_by_role("tab").all_inner_texts()
                         if name.strip().lower() in allowed]
                print("Plan tabs discovered:", names)
                if not names:
                    raise ValueError("No recognized plan tabs found. Existing data kept; use explore_website.py to inspect this page.")
                collected = list(plans)
                for name in names:
                    tab = page.get_by_role("tab", name=name, exact=True)
                    if tab.get_attribute("aria-selected") != "true":
                        previous_text = cards.all_inner_texts()
                        tab.click()
                        # Refuse to save an old tab's content under a newly selected view.
                        page.wait_for_function(
                            """old => {
                                const cards = [...document.querySelectorAll('[class*="planDetailsCard_container"]')];
                                return cards.length > 0 && JSON.stringify(cards.map(c=>c.innerText)) !== JSON.stringify(old);
                            }""", arg=previous_text, timeout=30000)
                    cards.first.locator('[class*="DataCol_font"]').wait_for(state="visible", timeout=45000)
                    batch = cards.evaluate_all("""cards => cards.map(card => ({
                        price: card.querySelector('h3[class*="PlanName_planText"]')?.innerText.trim() ?? null,
                        validity: card.querySelector('[class*="ValidityCol_gridItem"] .j-text-body-xxs-bold')?.innerText.trim() ?? null,
                        data_and_speed: card.querySelector('[class*="DataCol_font"]')?.innerText.trim() ?? null
                    }))""")
                    if not batch or any(not plan.get(field) for plan in batch for field in required):
                        raise ValueError(f"Incomplete plans in {name}; existing data kept")
                    collected.extend(batch)
                    print(f"Tab {name}: {len(batch)} plans")
                plans = list({tuple(plan[field] for field in required): plan for plan in collected}.values())

            from database import create_tables, save_plans
            create_tables()
            save_plans("JioFiber", plans, page.url)
            success = True

        finally:
            if browser is not None:
                start = perf_counter()
                browser.close()
                timings["Browser cleanup"] = perf_counter() - start

finally:
    elapsed = perf_counter() - total_start

    print("\nMode:", "Asset blocking" if BLOCK_ASSETS else "Baseline")
    print("Extraction status:", "SUCCESS" if success else "FAILED")

    for label, seconds in timings.items():
        print(f"{label}: {seconds:.3f} seconds")

    print(f"Total measured time: {elapsed:.3f} seconds")


print(f"\nCaptured plans: {len(plans)}")

for index, plan in enumerate(plans, start=1):
    # ASCII output avoids the PowerShell rupee-symbol issue.
    price = plan["price"].replace("\u20b9", "INR ")

    print(
        f"{index}. {price} | "
        f"{plan['validity']} | "
        f"{plan['data_and_speed']}"
    )