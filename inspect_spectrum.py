"""Inspect Spectrum address availability; save diagnostic captures, not plans.db.

python inspect_spectrum.py --address "43 W 23rd St" --zip "10010"
Default: visible local Chromium; install with: python -m playwright install chromium
Optional: --browser browserbase, --headless, --unit, --wait-seconds 0
0 disables the result wait deadline, NOT Browserbase's finite session lifetime.
No price is treated as an address-confirmed offer without adapter validation.
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
START_URL = "https://www.spectrum.com/internet"
NEGATIVE = "This address is not part of the Spectrum services area"


def classify(text):
    value = " ".join(text.lower().split())
    if any(
        s in value
        for s in (
            "verify you are human",
            "verify that you are human",
            "access denied",
            "unusual traffic",
            "complete the captcha",
        )
    ):
        return "verification_required"
    if NEGATIVE.lower() in value:
        return "unavailable"
    if any(
        s in value
        for s in (
            "please select an apartment",
            "please select your apartment",
            "please enter your apartment",
            "select your unit",
            "apartment number is required",
        )
    ):
        return "unit_required"
    return None


def first_visible(locator):
    for item in locator.all():
        if item.is_visible():
            return item
    return None


def visible_text(page):
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(frame.locator("body").inner_text(timeout=1500))
        except Exception:
            continue
    return "\n".join(chunks)


def find_field(page, kind):
    # Exact strings avoid slash serialization problems in role regex selectors.
    if kind == "unit":
        names = (
            "Apt/Unit",
            "Apt/Unit (optional)",
            "Apartment/Unit",
            "Apartment/Unit (optional)",
        )
        for name in names:
            for role in ("combobox", "textbox"):
                item = first_visible(page.get_by_role(role, name=name, exact=True))
                if item is not None:
                    return item
            item = first_visible(page.get_by_placeholder(name, exact=True))
            if item is not None:
                return item
        return None
    patterns = {"street": r"^street address$", "zip": r"^zip\s*code$"}
    name = re.compile(patterns[kind], re.I)
    for role in ("combobox", "textbox"):
        item = first_visible(page.get_by_role(role, name=name))
        if item is not None:
            return item
    return first_visible(page.get_by_placeholder(name))


def find_submit(page, name):
    for role in ("button", "link"):
        item = first_visible(page.get_by_role(role, name=re.compile(name, re.I)))
        if item is not None:
            return item
    return None


def choose_suggestion(page, address, zip_code, unit):
    """Select a unique matching suggestion only; never guess between apartments."""
    matches = []
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
    for item in page.get_by_role("option").all():
        if not item.is_visible():
            continue
        label = item.inner_text().strip()
        if normalize(address) not in normalize(label) or zip_code not in label:
            continue
        if unit:
            if not re.search(r"\b" + re.escape(unit) + r"\b", label, re.I):
                continue
        elif re.search(r"\b(?:apt|unit|suite|ste)\b", label, re.I):
            continue
        matches.append((item, label))
    if len(matches) == 1:
        matches[0][0].click()
        print("Selected matching address suggestion.", flush=True)
        return True
    return False


def handle_unit(page, unit, result):
    """Use the captured Angular autocomplete; require an exact supplied unit."""
    field = find_field(page, "unit")
    if field is None:
        result["message"] = "Unit input not found; inspect the saved HTML."
        return False
    field.click(timeout=5000)
    page.wait_for_timeout(500)

    def options():
        return [
            (item, item.inner_text().strip())
            for item in page.get_by_role("option").all()
            if item.is_visible()
        ]

    choices = options()
    result["available_units"] = list(dict.fromkeys(label for _, label in choices))
    if not unit:
        result["message"] = (
            "Supply --unit with the exact unit for this address. Available options captured."
        )
        return False
    field.fill(unit, timeout=5000)
    page.wait_for_timeout(1000)
    choices = options()
    normalize = lambda value: " ".join(value.casefold().split())
    matches = [item for item, label in choices if normalize(label) == normalize(unit)]
    if len(matches) != 1:
        result["message"] = (
            "Supplied unit did not match exactly one dropdown option; no unit guessed."
        )
        result["matching_unit_options"] = [label for _, label in choices]
        return False
    matches[0].click(timeout=5000)
    button = find_submit(page, r"^find offers$")
    if button is None:
        result["message"] = "Unit selected but Find offers was not found."
        return False
    button.click(timeout=10000)
    print("Selected matching unit and clicked Find offers.", flush=True)
    return True


def expired(started, seconds, now=None):
    return (
        seconds > 0 and (time.monotonic() if now is None else now) - started >= seconds
    )


def save_captures(page, output, result):
    if page is None:
        return
    result["source_url"] = page.url
    if page.is_closed():
        return  # Preserve the last successful captures.
    for name, read in [
        ("spectrum_page.html", page.content),
        ("spectrum_page_text.txt", lambda: visible_text(page)),
        (
            "spectrum_controls.json",
            lambda: json.dumps(
                page.locator(
                    'input, button, select, [role="option"], [role="alert"]'
                ).evaluate_all(
                    """items => items.map(e => ({
                           tag:e.tagName, role:e.getAttribute('role'),
                           label:e.getAttribute('aria-label'), placeholder:e.getAttribute('placeholder'),
                           text:e.innerText || '', value:e.value || '',
                           disabled:e.disabled || false, visible:!!e.getClientRects().length
                       }))"""
                ),
                indent=2,
            ),
        ),
    ]:
        try:
            value = read()
            if value:
                (output / name).write_text(value, encoding="utf-8")
        except Exception as error:
            result.setdefault("capture_errors", []).append(f"{name}: {error}")


def inspect(
    address,
    zip_code,
    unit="",
    wait_seconds=600,
    session_seconds=1800,
    browser_mode="local",
    headless=False,
):
    from playwright.sync_api import sync_playwright

    if browser_mode == "browserbase":
        from browserbase import Browserbase
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
        key, project = os.getenv("BROWSERBASE_API_KEY"), os.getenv(
            "BROWSERBASE_PROJECT_ID"
        )
        if not key or not project:
            raise ValueError(
                "Add BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID to .env"
            )
    started = time.perf_counter()
    output = (
        BASE_DIR
        / "captures"
        / ("spectrum-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    )
    output.mkdir(parents=True)
    result = {
        "provider": "Spectrum",
        "street_address": address,
        "zip_code": zip_code,
        "apartment": unit,
        "status": "incomplete",
        "service_available": None,
        "plans": [],
        "currency": "USD",
        "source_url": START_URL,
        "wait_seconds": wait_seconds,
        "browser_mode": browser_mode,
        "session_seconds": session_seconds if browser_mode == "browserbase" else None,
        "message": "Availability not determined.",
    }
    print("Spectrum inspector revision: local-browser-4", flush=True)
    print(f"Browser mode: {browser_mode}", flush=True)
    print(f'Result wait: {wait_seconds or "no script deadline"}', flush=True)
    try:
        if browser_mode == "browserbase":
            client = Browserbase(api_key=key)
            session = client.sessions.create(
                project_id=project, timeout=session_seconds, keep_alive=False
            )
            result["session_id"] = session.id
            print(f"Browserbase session ID: {session.id}", flush=True)
        with sync_playwright() as pw:
            browser = None
            page = None
            try:
                if browser_mode == "local":
                    browser = pw.chromium.launch(
                        headless=headless,
                        args=["--disable-quic"],
                )
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000}
                    )
                else:
                    browser = pw.chromium.connect_over_cdp(
                        session.connect_url, timeout=120000
                    )
                    context = browser.contexts[0]
                page = context.new_page()
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.set_default_timeout(60000)
                page.set_default_navigation_timeout(120000)
                print("Opening Spectrum...", flush=True)
                response = page.goto(START_URL, wait_until="domcontentloaded")
                if response is None or not response.ok:
                    raise RuntimeError("Spectrum returned an unsuccessful initial page")
                text = visible_text(page)
                if classify(text) == "verification_required":
                    result.update(
                        status="verification_required",
                        message="Spectrum security check; no bypass attempted.",
                    )
                    return result
                # Wait for the relevant visible input, not a potentially hidden first form.
                form_start = time.monotonic()
                while True:
                    street, zip_input = find_field(page, "street"), find_field(
                        page, "zip"
                    )
                    if street is not None and zip_input is not None:
                        break
                    if expired(form_start, 120):
                        raise RuntimeError(
                            "Address form not found in 120 seconds. Inspect saved controls."
                        )
                    page.wait_for_timeout(500)
                street.fill(address)
                zip_input.fill(zip_code)
                if unit:
                    unit_input = find_field(page, "unit")
                    if unit_input is None:
                        raise RuntimeError("Apartment/unit input not found")
                    unit_input.fill(unit)
                # Suggestions can arrive asynchronously; the result loop checks again.
                choose_suggestion(page, address, zip_code, unit)
                button = find_submit(page, r"^check availability$")
                if button is None:
                    raise RuntimeError("Check availability control not found")
                button.click()
                print("Address submitted; waiting for Spectrum result...", flush=True)
                wait_started = time.monotonic()
                next_progress = 0
                submitted_find_offers = False
                selected_suggestion = False
                unit_attempted = False
                next_capture = 0
                while not expired(wait_started, wait_seconds):
                    if page.is_closed():
                        raise RuntimeError(
                            "Browser page closed or remote session ended"
                        )
                    host = (urlsplit(page.url).hostname or "").lower()
                    if host != "spectrum.com" and not host.endswith(".spectrum.com"):
                        result.update(
                            status="external_redirect",
                            message="Browser left Spectrum; no other provider data imported.",
                        )
                        break
                    elapsed = time.monotonic() - wait_started
                    if elapsed >= next_capture:
                        save_captures(page, output, result)
                        next_capture = elapsed + 20
                    text = visible_text(page)
                    status = classify(text)
                    if (
                        urlsplit(page.url).path.rstrip("/") == "/address/multiple-unit"
                        and status != "verification_required"
                    ):
                        status = "unit_required"
                    if status == "unit_required":
                        result.update(status=status, service_available=None)
                        if not unit_attempted:
                            unit_attempted = True
                            if handle_unit(page, unit, result):
                                result.update(
                                    status="incomplete",
                                    message="Unit submitted; awaiting result.",
                                )
                                page.wait_for_timeout(1500)
                                continue
                        else:
                            result["message"] = (
                                "Spectrum still requires a unit after submission; inspect captures."
                            )
                        break
                    if status:
                        result["status"] = status
                        result["service_available"] = (
                            False if status == "unavailable" else None
                        )
                        result["message"] = {
                            "unavailable": NEGATIVE,
                            "unit_required": "Spectrum requires an apartment/unit.",
                            "verification_required": "Spectrum requires verification; no bypass attempted.",
                        }[status]
                        break
                    elapsed = time.monotonic() - wait_started
                    processing = "processing your request" in text.lower()
                    if elapsed >= next_progress:
                        state = (
                            "Spectrum processing/security countdown"
                            if processing
                            else "Waiting for address confirmation or offers"
                        )
                        print(f"{elapsed:.0f}s: {state}", flush=True)
                        next_progress = elapsed + 15
                    if not processing:
                        if not selected_suggestion:
                            selected_suggestion = choose_suggestion(
                                page, address, zip_code, unit
                            )
                        if "/address/" in urlsplit(page.url).path:
                            find_offers = find_submit(page, r"^find offers$")
                            if (
                                find_offers is not None
                                and find_offers.is_enabled()
                                and not submitted_find_offers
                            ):
                                find_offers.click()
                                submitted_find_offers = True
                                print("Clicked FIND OFFERS.", flush=True)
                                page.wait_for_timeout(1000)
                                continue
                        # Capture possible localized offers even if the site keeps an /address/ URL.
                        # A distinct result view must exist; initial marketing cards never qualify.
                        form_visible = find_field(page, "street") is not None
                        if (
                            urlsplit(page.url).path.rstrip("/") != "/internet"
                            and not form_visible
                            and re.search(r"\$\s*\d", text)
                            and re.search(r"Mbps|Gbps", text, re.I)
                        ):
                            result.update(
                                status="needs_review",
                                message="Possible localized offers captured. Validate address and plan selectors before importing.",
                            )
                            break
                    page.wait_for_timeout(1000)
                else:
                    result.update(
                        status="incomplete",
                        message=f"No recognized result after {wait_seconds} seconds. Review saved HTML, text and controls; no availability inferred.",
                    )
            except KeyboardInterrupt:
                result.update(
                    status="cancelled",
                    message="Stopped by user; partial captures saved where possible.",
                )
            except Exception as error:
                result.update(status="error", message=str(error))
            finally:
                save_captures(page, output, result)
                if browser is not None:
                    try:
                        browser.close()
                    except Exception as error:
                        result["cleanup_error"] = str(error)
    except KeyboardInterrupt:
        result.update(status="cancelled", message="Stopped by user.")
    except Exception as error:
        result.update(status="error", message=str(error))
    finally:
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        (output / "spectrum_result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, indent=2), flush=True)
        print("Saved captures:", output, flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--address", required=True)
    parser.add_argument("--zip", required=True, dest="zip_code")
    parser.add_argument("--unit", default="")
    parser.add_argument("--browser", choices=("local", "browserbase"), default="local")
    parser.add_argument(
        "--headless", action="store_true", help="Hide local browser window"
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=600,
        help="Result wait; 0 disables script deadline",
    )
    parser.add_argument(
        "--session-seconds",
        type=int,
        default=1800,
        help="Browserbase lifetime; 60..21600, subject to account limits",
    )
    args = parser.parse_args()
    if not args.address.strip() or not re.fullmatch(r"\d{5}", args.zip_code):
        parser.error("Provide a street address and a five-digit ZIP code")
    if args.unit.strip().upper() in {"EXACT_UNIT", "ACTUAL_UNIT", "ACTUAL UNIT"}:
        parser.error(
            "--unit must be a real unit at this address. Omit it to inspect available units."
        )
    if args.wait_seconds < 0 or (
        args.browser == "browserbase" and not 60 <= args.session_seconds <= 21600
    ):
        parser.error("Wait must be >=0; session must be 60..21600 seconds")
    if (
        args.browser == "browserbase"
        and args.wait_seconds
        and args.session_seconds < args.wait_seconds + 180
    ):
        parser.error(
            "Session must exceed result wait by at least 180 seconds for setup"
        )
    try:
        result = inspect(
            args.address.strip(),
            args.zip_code,
            args.unit.strip(),
            args.wait_seconds,
            args.session_seconds,
            args.browser,
            args.headless,
        )
    except ImportError as error:
        print(
            f"Missing dependency: {error}. Run: python -m pip install browserbase python-dotenv playwright"
        )
        return 1
    except ValueError as error:
        print(error)
        return 1
    return (
        0 if result["status"] in {"unavailable", "unit_required", "needs_review"} else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
