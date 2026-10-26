"""ACT Chennai main-table scraper. Offline check: --html captures/act_page.html"""
import argparse
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

URL = "https://www.actcorp.in/broadband/chennai/broadband-plans"
BASE_DIR = Path(__file__).resolve().parent


class PlanTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.row_depth = None
        self.row_text = []
        self.heading = None
        self.price = None
        self.pack = None
        self.prices = []
        self.plans = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            return
        self.depth += 1
        classes = attrs.get("class", "").split()
        if tag == "div" and "select-plans-table" in classes:
            if self.row_depth is not None:
                raise ValueError("Unexpected nested plan table row")
            self.row_depth = self.depth
            self.pack = None
            self.prices = []
        if self.row_depth is not None:
            if tag == "h4" and self.pack is None:
                self.heading = []
            if tag == "h5" and "desktop-price" in classes:
                self.price = [attrs.get("plan-speed"), []]

    def handle_startendtag(self, tag, attrs):
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        if self.price is not None:
            self.price[1].append(data)

    def handle_endtag(self, tag):
        if tag == "h4" and self.heading is not None:
            self.pack = " ".join(" ".join(self.heading).split())
            self.heading = None
        if tag == "h5" and self.price is not None:
            speed, parts = self.price
            text = " ".join(parts)
            match = re.fullmatch(r"\s*₹\s*([\d,]+(?:\.\d{1,2})?)\s*", text)
            if not match or not speed or float(speed) <= 0:
                raise ValueError(f"Invalid table price/speed: {text!r}, {speed!r}")
            self.prices.append({
                "advertised_price_inr": match.group(1).replace(",", ""),
                "speed_mbps": float(speed),
                "billing_period": None,
                "conditions": [f"Pack: {self.pack}",
                               "Billing period and taxes not verified; confirm before comparing monthly costs.",
                               "Smart Mesh routers available with select plans; view details on next page."],
            })
            self.price = None
        if self.row_depth == self.depth and tag == "div":
            if self.pack and not self.prices:
                raise ValueError("Incomplete main plan table row; existing data kept")
            self.plans.extend(self.prices)
            self.row_depth = None
        self.depth -= 1


def extract_plans(html):
    parser = PlanTableParser()
    parser.feed(html)
    plans = parser.plans
    # This capture has four packs and seven speeds. Fail closed if structure changes.
    packs = {}
    for plan in plans:
        pack = plan["conditions"][0]
        speeds = packs.setdefault(pack, set())
        if plan["speed_mbps"] in speeds:
            raise ValueError("Duplicate speed within a pack; existing data kept")
        speeds.add(plan["speed_mbps"])
    if len(packs) != 4 or any(len(speeds) != 7 for speeds in packs.values()):
        raise ValueError("Expected four packs with seven speeds each; inspect fresh HTML before saving")
    if len({tuple(sorted(speeds)) for speeds in packs.values()}) != 1:
        raise ValueError("Pack speed columns do not match")
    return plans


def main():
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--html", type=Path, help="Validate a saved capture without network or database writes")
    args = args_parser.parse_args()
    start = perf_counter()
    if args.html:
        plans = extract_plans(args.html.read_text(encoding="utf-8"))
    else:
        from browserbase import Browserbase
        from dotenv import load_dotenv
        from playwright.sync_api import sync_playwright
        from database import save_provider_plans

        load_dotenv(BASE_DIR / ".env")
        api_key = os.getenv("BROWSERBASE_API_KEY")
        project_id = os.getenv("BROWSERBASE_PROJECT_ID")
        if not api_key or not project_id:
            raise ValueError("Add Browserbase credentials to .env")
        session = Browserbase(api_key=api_key).sessions.create(
            project_id=project_id, timeout=180, keep_alive=False)
        with sync_playwright() as playwright:
            browser = None
            try:
                browser = playwright.chromium.connect_over_cdp(session.connect_url, timeout=60000)
                page = browser.contexts[0].new_page()
                page.set_viewport_size({"width": 1440, "height": 1000})
                response = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                if response is None or not response.ok:
                    raise RuntimeError("ACT returned an unsuccessful response")
                if urlsplit(page.url).path.rstrip("/") != urlsplit(URL).path:
                    raise RuntimeError("ACT redirected away from Chennai; existing data kept")
                page.locator(".select-plans-table h5.desktop-price[plan-speed]").first.wait_for(state="attached", timeout=45000)
                page.wait_for_function(r"""() => {
                    const rows = [...document.querySelectorAll('.select-plans-table')];
                    const packs = rows.filter(r => r.querySelector('h4'));
                    return packs.length === 4 && packs.every(r => {
                        const prices = [...r.querySelectorAll('h5.desktop-price[plan-speed]')];
                        return prices.length === 7 && prices.every(p => /₹\s*[\d,]+/.test(p.textContent));
                    });
                }""", timeout=45000)
                html = page.content()
                captures = BASE_DIR / "captures"
                captures.mkdir(exist_ok=True)
                (captures / "act_scraped_page.html").write_text(html, encoding="utf-8")
                plans = extract_plans(html)
                save_provider_plans("ACT", "Chennai", plans, URL)
            finally:
                if browser is not None:
                    browser.close()
    print(f"Captured ACT Chennai main-table plans: {len(plans)}")
    for plan in plans:
        print(f"{plan['conditions'][0]} | INR {plan['advertised_price_inr']} | {plan['speed_mbps']:g} Mbps")
    print("Billing period: not confirmed")
    print(f"Total time: {perf_counter() - start:.2f} seconds")


if __name__ == "__main__":
    main()
