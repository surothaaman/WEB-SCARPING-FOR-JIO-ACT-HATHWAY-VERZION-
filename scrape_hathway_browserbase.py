import os
import re
from pathlib import Path
from time import perf_counter
from database import save_provider_plans

from browserbase import Browserbase
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

URL = "https://hathwaybroadband.com/internet-service-provider/chennai/"

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

        if response is None or not response.ok:
            raise RuntimeError("Hathway returned an unsuccessful response.")

        # These selectors come from your captured HTML.
        headings = page.locator("#Plans .planList > li h3")
        headings.first.wait_for(state="visible", timeout=30000)

        raw_plans = headings.all_inner_texts()

        conditions = page.locator(
            "#Plans .subInfo > li"
        ).all_inner_texts()

        plans = []

        for raw_text in raw_plans:
            text = " ".join(raw_text.split())

            speed_match = re.search(
                r"(\d+(?:\.\d+)?)\s*Mbps",
                text,
                re.IGNORECASE,
            )

            price_match = re.search(
                r"₹\s*([\d,]+(?:\.\d{1,2})?)",
                text,
            )

            if not speed_match or not price_match:
                raise ValueError(
                    f"Could not extract price or speed from: {text!r}"
                )

            plans.append({
                "provider": "Hathway",
                "city": "Chennai",
                "advertised_price_inr": price_match.group(1).replace(",", ""),
                "speed_mbps": float(speed_match.group(1)),
                "billing_period": None,
                "conditions": [item.strip() for item in conditions],
                "source_url": page.url,
            })

        if not plans:
            raise ValueError("No Hathway plans were extracted.")

        print(f"Captured Hathway plans: {len(plans)}")

        for index, plan in enumerate(plans, start=1):
            print(
                f"{index}. INR {plan['advertised_price_inr']} | "
                f"{plan['speed_mbps']:g} Mbps | "
                "Billing period: not confirmed"
            )

        print("\nConditions:")
        for condition in conditions:
            print("-", condition.strip())
        save_provider_plans(
            provider="Hathway",
            city="Chennai",
            plans=plans,
            source_url=page.url,
        )
    finally:
        if browser is not None:
            browser.close()

print(f"\nTotal scraping time: {perf_counter() - start:.2f} seconds")