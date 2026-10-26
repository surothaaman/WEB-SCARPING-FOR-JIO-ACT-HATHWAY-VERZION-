"""Inspect Verizon residential home-internet availability for one address.

Example:
    python inspect_verizon.py --address "123 Main St, City, ST 12345"
    python inspect_verizon.py --address "123 Main St, City, ST" --zip 12345 --headless

The script saves diagnostic captures under ./captures. It does not write to plans.db.
It only extracts offers from Verizon's address-check result widget; generic page
marketing is never treated as address-confirmed availability.
"""

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
START_URL = "https://www.verizon.com/home/internet/"
ALLOWED_HOSTS = ("verizon.com",)
SCRIPT_REVISION = "verizon-sales-cards-fix-6"


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def compose_address(address: str, zip_code: str | None) -> str:
    value = address.strip()
    if zip_code and not re.search(rf"\b{re.escape(zip_code)}\b", value):
        value = f"{value}, {zip_code}"
    return value


def page_text(page) -> str:
    chunks = []
    for frame in page.frames:
        try:
            chunks.append(frame.locator("body").inner_text(timeout=1500))
        except Exception:
            continue
    return "\n".join(chunks)


def result_widget_text(page) -> str:
    """Read only the visible address-check UI, not the page's marketing cards."""
    selectors = (
        "#lq-widget-root",
        "#lq20",
        ".lq20",
        '[id*="lq-widget"]',
        '[id*="lq20"]',
        '[role="dialog"]',
        '[aria-modal="true"]',
    )
    chunks = []
    seen = set()
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 10)
            for index in range(count):
                item = locator.nth(index)
                if not item.is_visible():
                    continue
                text = item.inner_text(timeout=1000).strip()
                if text and text not in seen:
                    seen.add(text)
                    chunks.append(text)
        except Exception:
            continue
    return "\n\n".join(chunks)


def find_address_input(page, browser, timeout_seconds: float = 25):
    """Find the street field even when Verizon renders its form in an iframe."""
    selectors = (
        "#streetaddress",
        "#sticky-lq-adress",
        'input[autocomplete*="street-address" i]',
        'input[id*="street" i]',
        'input[name*="street" i]',
        'input[placeholder*="address" i]',
        'input[aria-label*="address" i]',
        'input[name*="address" i]',
        'input[id*="address" i]',
    )
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    clicked_launchers = set()
    while time.monotonic() < deadline:
        if page.is_closed() or not browser.is_connected():
            raise RuntimeError(
                "The browser or page closed before an editable address form appeared."
            )
        for frame in page.frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector)
                    for index in range(min(locator.count(), 5)):
                        candidate = locator.nth(index)
                        if candidate.is_visible() and candidate.is_enabled():
                            if candidate.is_editable():
                                return candidate
                            # The captured sticky-lq-adress input is read-only:
                            # it launches the real address widget when clicked.
                            element_id = candidate.get_attribute("id")
                            key = (
                                (frame.url, element_id)
                                if element_id
                                else (frame.url, selector, index)
                            )
                            if (
                                candidate.get_attribute("readonly") is not None
                                and key not in clicked_launchers
                            ):
                                clicked_launchers.add(key)
                                print(
                                    "Opening the read-only address launcher...",
                                    flush=True,
                                )
                                candidate.click(timeout=3000)
                except Exception as error:
                    last_error = error
            # Some versions render the field with generated IDs and no placeholder.
            # Inspect only visible inputs whose nearby label indicates an address.
            try:
                inputs = frame.locator("input")
                metadata = inputs.evaluate_all("""items => items.map((e, index) => ({
                        index,
                        visible: !!e.getClientRects().length,
                        disabled: !!e.disabled,
                        hint: [e.id, e.name, e.placeholder, e.getAttribute('aria-label'),
                            e.getAttribute('autocomplete'), e.labels ? [...e.labels].map(x => x.innerText).join(' ') : '',
                            e.parentElement ? e.parentElement.innerText : '']
                            .filter(Boolean).join(' ').slice(0, 500)
                    }))""")
                for item in metadata:
                    hint = item["hint"].casefold()
                    if (
                        item["visible"]
                        and not item["disabled"]
                        and re.search(r"street|address|location", hint)
                        and not re.search(r"zip|postal|email", hint)
                    ):
                        candidate = inputs.nth(item["index"])
                        if candidate.is_editable():
                            return candidate
            except Exception as error:
                last_error = error
        page.wait_for_timeout(300)
    raise TimeoutError(
        "No editable Verizon street-address input appeared in the main page or its frames. "
        f"URL={page.url!r}; frames={len(page.frames)}; last selector error={last_error!s}"
    )


def collect_address_options(page):
    """Collect visible autocomplete options across the main document and frames."""
    labels = []
    for frame in page.frames:
        try:
            options = frame.get_by_role("option")
            for index in range(min(options.count(), 20)):
                option = options.nth(index)
                if option.is_visible():
                    label = option.inner_text(timeout=1000).strip()
                    if label and label not in labels:
                        labels.append(label)
        except Exception:
            continue
    return labels


def address_tokens(value: str) -> list[str]:
    value = re.sub(r"\bunited\s+states(?:\s+of\s+america)?\b", "", normalize(value))
    aliases = {
        "place": "pl",
        "avenue": "ave",
        "street": "st",
        "drive": "dr",
        "road": "rd",
        "boulevard": "blvd",
        "lane": "ln",
        "court": "ct",
        "apartment": "apt",
    }
    return [
        aliases.get(token, token)
        for token in re.findall(r"[a-z0-9]+", value)
        if token not in {"usa", "us"}
    ]


def suggestion_matches_address(address: str, suggestion: str) -> bool:
    expected = address_tokens(address)
    actual = address_tokens(suggestion)
    # Require the same house number and every supplied address component.
    return (
        len(expected) >= 3
        and bool(actual)
        and expected[0] == actual[0]
        and set(expected).issubset(actual)
    )


def unique_matching_suggestion(address: str, labels: list[str]):
    matches = [label for label in labels if suggestion_matches_address(address, label)]
    unique = {tuple(address_tokens(label)) for label in matches}
    return matches[0] if matches and len(unique) == 1 else None


def click_address_suggestion(page, label):
    for frame in page.frames:
        options = frame.get_by_role("option")
        for index in range(min(options.count(), 20)):
            option = options.nth(index)
            if option.is_visible() and option.inner_text(timeout=1000).strip() == label:
                option.click(timeout=5000)
                return
    raise RuntimeError(
        "The matching address suggestion disappeared before selection. Please retry the lookup."
    )


def find_get_started_control(page):
    for frame in page.frames:
        candidates = (
            frame.locator("#stickyBar-CTA").first,
            frame.get_by_role(
                "link", name=re.compile(r"get started|check availability", re.I)
            ).first,
            frame.get_by_role(
                "button", name=re.compile(r"get started|check availability", re.I)
            ).first,
        )
        for locator in candidates:
            try:
                if locator.count() and locator.is_visible() and locator.is_enabled():
                    return locator
            except Exception:
                continue
    return None


def classify_result(text: str) -> tuple[str | None, str | None]:
    value = normalize(text)
    if any(
        phrase in value
        for phrase in (
            "verify you are human",
            "verify that you are human",
            "checking your browser",
            "complete the captcha",
            "unusual traffic",
        )
    ):
        return (
            "verification_required",
            "Verizon presented a security check; no bypass attempted.",
        )

    if (
        "we’re unable to continue your order at this time" in value
        or "we're unable to continue your order at this time" in value
    ):
        return (
            "needs_support",
            "Verizon could not continue the online order flow; availability was not confirmed.",
        )

    if any(
        phrase in value
        for phrase in (
            "home internet is not available at this address",
            "internet service is not available at this address",
            "we do not offer home internet at this address",
            "we don't offer home internet at this address",
            "no home internet service is available at this address",
        )
    ):
        return (
            "unavailable",
            "Verizon explicitly reported no home internet service at this address.",
        )

    if any(
        phrase in value
        for phrase in (
            "please select an apartment",
            "select your unit",
            "unit number is required",
            "apartment number is required",
        )
    ):
        return "unit_required", "Verizon requires an apartment or unit selection."

    return None, None


def parse_address_result_offers(text: str) -> list[dict]:
    """Extract price/speed pairs from result-widget text only."""
    offers = []
    price_re = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:/\s*(?:mo|month))?", re.I)
    speed_re = re.compile(r"\b(\d+(?:\.\d+)?)\s*(Gbps|Mbps)\b", re.I)
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    for block in blocks:
        price = price_re.search(block)
        speed = speed_re.search(block)
        if not price or not speed:
            continue
        lines = [line.strip(" \t:-") for line in block.splitlines() if line.strip()]
        name = lines[0][:100] if lines else "Verizon home internet offer"
        speed_value = float(speed.group(1))
        speed_unit = speed.group(2).upper()
        speed_label = f"{int(speed_value) if speed_value.is_integer() else speed_value} {speed_unit}"
        offers.append(
            {
                "name": name,
                "monthly_price_usd": float(price.group(1)),
                "speed": speed_label,
                "raw_text": block[:1200],
            }
        )
    # Keep stable order and remove duplicate cards that may be repeated in the DOM.
    unique = []
    seen = set()
    for offer in offers:
        key = (offer["name"], offer["monthly_price_usd"], offer["speed"])
        if key not in seen:
            seen.add(key)
            unique.append(offer)
    return unique


def parse_sales_result(text: str, submitted_address: str) -> dict:
    """Parse the sales layout validated against the supplied October 23 capture."""
    result = {"plans": [], "service_available": None}
    # Only the sales section before Broadband Facts contains advertised cards.
    cards_text = text.split("Home broadband facts", 1)[0]
    headings = list(
        re.finditer(r"(?m)^\s*(5G Home Ultimate|5G Home Plus|5G Home)\s*$", cards_text)
    )
    conditions_match = re.search(
        r"Plan prices include taxes & fees\.(.*?)(?:\n\s*\*\*|\n\s*1 Disney|$)",
        cards_text,
        re.S,
    )
    conditions = (
        "Plan prices include taxes & fees. " + conditions_match.group(1).strip()
        if conditions_match
        else ""
    )
    names_seen = set()
    for index, heading in enumerate(headings):
        name = heading.group(1)
        if name in names_seen:
            continue
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(cards_text)
        )
        card = cards_text[heading.start() : end].split("Or, choose", 1)[0]
        price = re.search(r"\$\s*(\d+(?:\.\d+)?)\s*(?:per month|/\s*mo)", card, re.I)
        speed = re.search(
            r"Up to\s+(\d+(?:\.\d+)?)\s*(Mbps|Gbps)\s+download", card, re.I
        )
        if not price or not speed:
            continue
        bundle = re.search(r"As low as\s*\$\s*(\d+(?:\.\d+)?)", card, re.I)
        typical = re.search(
            r"Typical speeds of\s*([\d.]+\s*-\s*[\d.]+\s*Mbps)", card, re.I
        )
        label = re.search(
            r"Broadband Facts\s+Verizon\s+"
            + re.escape(name)
            + r"\s+up to\s+[\d.]+\s*Mbps\s+Fixed Broadband Consumer Disclosure\s+Monthly Price\s+\$\s*(\d+(?:\.\d+)?)",
            text,
            re.I,
        )
        result["plans"].append(
            {
                "name": name,
                "monthly_price_usd": float(price.group(1)),
                "mobile_bundle_price_usd": float(bundle.group(1)) if bundle else None,
                "broadband_label_monthly_price_usd": (
                    float(label.group(1)) if label else None
                ),
                "speed": f"{speed.group(1)} {speed.group(2)}",
                "typical_download_speed": typical.group(1) if typical else None,
                "billing_period": "Monthly",
                "price_conditions": conditions,
                "address_confirmed": False,
                "raw_text": card.strip()[:6000],
            }
        )
        names_seen.add(name)
    address_match = re.search(
        r"Good news, Verizon Home Internet is available\. Choose your plan\.\s*\n\s*([^\n]+)\s*\nChange\b",
        text,
        re.I,
    )
    if not address_match:
        if result["plans"]:
            result.update(
                status="needs_review",
                message="Sales plan cards were captured, but their resolved service address was not found.",
            )
        return result
    resolved = address_match.group(1).strip()
    result["resolved_address"] = resolved
    unit_pattern = r"(?:\b(?:apt|apartment|unit|suite)\b|#)\s*([^,\n]+)"
    resolved_unit = re.search(unit_pattern, resolved, re.I)
    supplied_unit = re.search(unit_pattern, submitted_address, re.I)
    address_matches = suggestion_matches_address(submitted_address, resolved)
    unit_matches = (not resolved_unit and not supplied_unit) or (
        resolved_unit
        and supplied_unit
        and address_tokens(resolved_unit.group(1))
        == address_tokens(supplied_unit.group(1))
    )
    if not address_matches:
        result.update(
            status="address_mismatch",
            message=f"Verizon displayed a different service address: {resolved}. Captured plans require address review.",
        )
    elif not unit_matches:
        result.update(
            status="unit_confirmation_required",
            message=f"Verizon resolved this lookup to {resolved}. Confirm the apartment/unit before treating these plans as available for your home.",
        )
    else:
        result.update(
            status="available",
            service_available=True,
            message="Verizon confirmed the displayed service address and returned its residential plan cards.",
        )
        for plan in result["plans"]:
            plan["address_confirmed"] = True
    return result


def capture_page(page, output: Path, result: dict) -> None:
    if page is None:
        return
    try:
        if page.is_closed():
            result.setdefault("capture_errors", []).append(
                "Page was already closed; browser capture skipped."
            )
            return
        result["source_url"] = page.url
    except Exception as error:
        result.setdefault("capture_errors", []).append(
            f"Could not read page URL: {error}"
        )
        return

    try:
        (output / "verizon_page.html").write_text(page.content(), encoding="utf-8")
    except Exception as error:
        result.setdefault("capture_errors", []).append(f"verizon_page.html: {error}")

    try:
        (output / "verizon_page_text.txt").write_text(page_text(page), encoding="utf-8")
    except Exception as error:
        result.setdefault("capture_errors", []).append(
            f"verizon_page_text.txt: {error}"
        )

    try:
        controls = []
        for frame in page.frames:
            try:
                frame_controls = frame.locator(
                    "input, button, a, select, [role='option'], [role='alert']"
                ).evaluate_all(
                    """items => items.map(e => ({
                        tag: e.tagName,
                        role: e.getAttribute('role'),
                        id: e.id || '',
                        label: e.getAttribute('aria-label') || '',
                        placeholder: e.getAttribute('placeholder') || '',
                        text: (e.innerText || '').trim(),
                        value: e.value || '',
                        disabled: !!e.disabled,
                        visible: !!e.getClientRects().length
                    }))"""
                )
                controls.extend(
                    {"frame_url": frame.url, **item} for item in frame_controls
                )
            except Exception:
                continue
        (output / "verizon_controls.json").write_text(
            json.dumps(controls, indent=2), encoding="utf-8"
        )
    except Exception as error:
        result.setdefault("capture_errors", []).append(
            f"verizon_controls.json: {error}"
        )


def inspect(
    address: str, zip_code: str | None, wait_seconds: int, headless: bool
) -> dict:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise ImportError(
            "Playwright is required. Install it with: "
            "python -m pip install playwright && python -m playwright install chromium"
        ) from error

    submitted_address = compose_address(address, zip_code)
    started = time.perf_counter()
    output = (
        BASE_DIR
        / "captures"
        / ("verizon-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    )
    output.mkdir(parents=True, exist_ok=True)

    result = {
        "provider": "Verizon",
        "inspector_revision": SCRIPT_REVISION,
        "service_type": "residential_home_internet",
        "address": submitted_address,
        "status": "incomplete",
        "service_available": None,
        "plans": [],
        "currency": "USD",
        "source_url": START_URL,
        "wait_seconds": wait_seconds,
        "message": "No address-specific result has been confirmed.",
    }

    browser = None
    context = None
    page = None
    browser_events = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    headless=headless,
                    args=["--disable-quic", "--deny-permission-prompts"],
                )
                browser.on(
                    "disconnected",
                    lambda: browser_events.append("browser disconnected"),
                )
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.on(
                    "page",
                    lambda popup: browser_events.append(
                        f"new page opened: {popup.url}"
                    ),
                )
                page = context.new_page()
                # Deny location permission: availability uses the supplied address.
                try:
                    cdp = context.new_cdp_session(page)
                    target = cdp.send("Target.getTargetInfo")["targetInfo"]
                    permission = {
                        "permission": {"name": "geolocation"},
                        "setting": "denied",
                        "origin": "https://www.verizon.com",
                    }
                    if target.get("browserContextId"):
                        permission["browserContextId"] = target["browserContextId"]
                    cdp.send("Browser.setPermission", permission)
                    cdp.detach()
                    result["geolocation_permission"] = "denied"
                except Exception as error:
                    browser_events.append(f"Location permission setup: {error}")
                page.on("close", lambda: browser_events.append("page closed"))
                page.on("crash", lambda: browser_events.append("page crashed"))
                page.set_default_timeout(15000)
                page.set_default_navigation_timeout(120000)
                print("Opening Verizon...", flush=True)
                page.goto(START_URL, wait_until="domcontentloaded", timeout=120000)
                # Do not enumerate every frame before entering the address. Some
                # unrelated frames can delay diagnostic reads by many seconds.
                try:
                    (output / "verizon_initial.html").write_text(
                        page.content(), encoding="utf-8"
                    )
                except Exception:
                    pass

                host = (urlsplit(page.url).hostname or "").lower()
                if not any(
                    host == domain or host.endswith("." + domain)
                    for domain in ALLOWED_HOSTS
                ):
                    result.update(
                        status="external_redirect",
                        message="Browser left Verizon; no other provider data was used.",
                    )
                else:
                    result["stage"] = "locate_editable_address_field"
                    print("Waiting for visible address field...", flush=True)
                    address_input = find_address_input(page, browser)
                    result["stage"] = "type_address"
                    print("Address field found; typing supplied address...", flush=True)
                    address_input.click()
                    address_input.fill("")
                    address_input.press_sequentially(submitted_address, delay=35)
                    page.wait_for_timeout(400)
                    filled_value = address_input.input_value().strip()
                    if normalize(filled_value) != normalize(submitted_address):
                        address_input.click()
                        address_input.fill(submitted_address)
                        page.wait_for_timeout(400)
                        filled_value = address_input.input_value().strip()
                    if normalize(filled_value) != normalize(submitted_address):
                        raise RuntimeError(
                            "Verizon changed or cleared the entered address before submission."
                        )
                    result["address_field_filled"] = True
                    result["address_field_value"] = filled_value
                    result["stage"] = "select_address_suggestion"
                    print("Address text verified in field.", flush=True)

                    # Select only a uniquely matching address, even if nearby homes
                    # also appear in the autocomplete dropdown.
                    page.wait_for_timeout(1200)
                    option_labels = collect_address_options(page)
                    chosen_label = unique_matching_suggestion(
                        submitted_address, option_labels
                    )
                    if option_labels and chosen_label is None:
                        result.update(
                            status="ambiguous_address",
                            message="No unique autocomplete suggestion matched all supplied address components.",
                            address_suggestions=option_labels,
                        )
                    else:
                        if chosen_label is not None:
                            click_address_suggestion(page, chosen_label)
                            result["selected_address"] = chosen_label
                            print(
                                f"Selected matching suggestion: {chosen_label}",
                                flush=True,
                            )

                        get_started = find_get_started_control(page)
                        if get_started is None:
                            raise RuntimeError(
                                "Verizon accepted the address text, but the Get started or Check availability control was not found."
                            )
                        get_started.click()
                        result["stage"] = "wait_for_address_result"
                        print(
                            "Address submitted; waiting for Verizon result content...",
                            flush=True,
                        )
                        if context.pages:
                            page = context.pages[-1]
                            page.on(
                                "close",
                                lambda: browser_events.append("active page closed"),
                            )
                            page.on(
                                "crash",
                                lambda: browser_events.append("active page crashed"),
                            )

                        deadline = time.monotonic() + wait_seconds
                        next_capture = 0.0
                        previous_plans = None
                        stable_plan_reads = 0
                        submitted_at = time.monotonic()
                        while time.monotonic() < deadline:
                            if page.is_closed() or not browser.is_connected():
                                result.update(
                                    status="browser_closed",
                                    message="The Playwright page closed before Verizon returned an availability result.",
                                )
                                break

                            host = (urlsplit(page.url).hostname or "").lower()
                            if not any(
                                host == domain or host.endswith("." + domain)
                                for domain in ALLOWED_HOSTS
                            ):
                                result.update(
                                    status="external_redirect",
                                    message="Browser left Verizon; no other provider data was used.",
                                )
                                break

                            widget_text = result_widget_text(page)
                            body_text = page.locator("body").inner_text(timeout=3000)
                            status, message = classify_result(body_text)
                            if status:
                                result.update(status=status, message=message)
                                if status == "unavailable":
                                    result["service_available"] = False
                                break

                            # The address checker may transition to a separate Verizon sales route.
                            route = urlsplit(page.url).path.rstrip("/").lower()
                            if route == "/sales/home/progressiveplan.html":
                                result["result_route"] = route
                                sales_result = parse_sales_result(
                                    body_text, submitted_address
                                )
                                sales_plans = sales_result.get("plans", [])
                                signature = json.dumps(sales_result, sort_keys=True)
                                stable_plan_reads = (
                                    stable_plan_reads + 1
                                    if sales_plans and signature == previous_plans
                                    else 0
                                )
                                previous_plans = signature
                                if sales_plans and stable_plan_reads >= 2:
                                    result.update(sales_result)
                                    result["stage"] = "plans_extracted"
                                    print(
                                        f"Captured {len(sales_plans)} Verizon plan cards; status: {result['status']}",
                                        flush=True,
                                    )
                                    break
                                page.wait_for_timeout(1000)
                                continue

                            # A route change is not a completed result. Wait for the
                            # sales content and require stable candidate offers.
                            offer_text = widget_text
                            if route == "/sales/home/progressiveplan.html":
                                offer_text = widget_text or body_text
                            plans = parse_address_result_offers(offer_text)
                            signature = json.dumps(plans, sort_keys=True)
                            stable_plan_reads = (
                                stable_plan_reads + 1
                                if plans and signature == previous_plans
                                else 0
                            )
                            previous_plans = signature
                            if (
                                plans
                                and stable_plan_reads >= 2
                                and time.monotonic() - submitted_at >= 3
                            ):
                                for plan in plans:
                                    plan["address_confirmed"] = False
                                result.update(
                                    status="needs_review",
                                    message="Verizon returned stable candidate offers. Address-specific plan wording still needs verification before these offers can be marked confirmed.",
                                    plans=plans,
                                )
                                break

                            full_text = normalize(body_text)
                            if (
                                result.get("selected_address")
                                and normalize(result["selected_address"])
                                in normalize(body_text)
                            ) and any(
                                phrase in full_text
                                for phrase in (
                                    "available at your address",
                                    "available at this address",
                                    "service is available for your address",
                                )
                            ):
                                result.update(
                                    status="available",
                                    service_available=True,
                                    message="Verizon displayed the selected address with an availability confirmation; waiting for plan details.",
                                )

                            elapsed = time.monotonic() - started
                            if elapsed >= next_capture:
                                capture_page(page, output, result)
                                next_capture = elapsed + 10
                            page.wait_for_timeout(1000)
                        else:
                            if result.get("service_available") is True:
                                result["message"] = (
                                    "Verizon confirmed availability, but no usable price/speed pairs appeared before the wait limit. Review the saved page text and controls."
                                )
                            else:
                                result.update(
                                    status="incomplete",
                                    message=f"No completed Verizon address result or stable plan details appeared within {wait_seconds} seconds. Review the saved page text and controls.",
                                )
            except PlaywrightTimeoutError as error:
                result.update(
                    status="timeout",
                    message=f"Verizon page control timed out: {error}",
                    error_type=type(error).__name__,
                    error_detail=str(error),
                )
            except Exception as error:
                closed = (
                    "TargetClosedError" in type(error).__name__
                    or "Target page, context or browser has been closed" in str(error)
                    or "closed before an editable address form appeared" in str(error)
                )
                result.update(
                    status="browser_closed" if closed else "error",
                    message=(
                        "The browser or page closed during the lookup. The cause was not established; "
                        "check stage, error_detail and browser_events. Availability remains unconfirmed."
                        if closed
                        else f"{type(error).__name__}: {error}"
                    ),
                    error_type=type(error).__name__,
                    error_detail=str(error),
                )
            finally:
                # Capture and close browser objects before Playwright's event loop exits.
                capture_page(page, output, result)
                if browser_events:
                    result["browser_events"] = browser_events
                if browser is not None:
                    try:
                        browser.close()
                    except Exception as error:
                        result["cleanup_error"] = str(error)
    except Exception as error:
        result.update(status="error", message=f"{type(error).__name__}: {error}")

    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    (output / "verizon_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, indent=2))
    print(f"Saved captures: {output}")
    return result


def recover_capture(text_path: str, address: str) -> dict:
    path = Path(text_path).resolve()
    original_path = path.with_name("verizon_result.json")
    if not original_path.exists():
        raise ValueError(
            "Keep verizon_result.json beside the captured text to verify the original address and capture time."
        )
    original = json.loads(original_path.read_text(encoding="utf-8-sig"))
    if address_tokens(original.get("address", "")) != address_tokens(address):
        raise ValueError(
            "The recovery address must match the original captured lookup address."
        )
    parsed = parse_sales_result(path.read_text(encoding="utf-8-sig"), address)
    if not parsed.get("plans"):
        raise ValueError(
            "No validated Verizon sales plan cards were found in this text capture."
        )
    result = {
        **original,
        **parsed,
        "inspector_revision": SCRIPT_REVISION,
        "stage": "plans_recovered_from_capture",
        "recovered_from_capture": str(path),
        "recovered_at": datetime.now(timezone.utc).isoformat(),
    }
    return result


def main() -> int:
    print(f"Verizon inspector revision: {SCRIPT_REVISION}")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--address",
        required=True,
        help="Street address; add city/state for best matching",
    )
    parser.add_argument(
        "--zip",
        dest="zip_code",
        help="Optional ZIP code appended if not already in --address",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=90,
        help="Maximum wait for an address result",
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run Chromium without a visible window"
    )
    parser.add_argument(
        "--capture-text",
        help="Recover plan cards from a saved verizon_page_text.txt without opening a browser",
    )
    parser.add_argument(
        "--save-db",
        action="store_true",
        help="Save this result to plans.db using database.py",
    )
    args = parser.parse_args()

    if not args.address.strip():
        parser.error("--address cannot be empty")
    if args.zip_code and not re.fullmatch(r"\d{5}(?:-\d{4})?", args.zip_code.strip()):
        parser.error(
            "--zip must be a five-digit ZIP code, optionally followed by four digits"
        )
    if args.wait_seconds < 10 or args.wait_seconds > 600:
        parser.error("--wait-seconds must be between 10 and 600")

    try:
        if args.capture_text:
            result = recover_capture(
                args.capture_text, compose_address(args.address, args.zip_code)
            )
        else:
            result = inspect(
                args.address.strip(),
                args.zip_code.strip() if args.zip_code else None,
                args.wait_seconds,
                args.headless,
            )
        if args.save_db:
            from database import create_tables, save_address_check

            create_tables()
            result["check_id"] = save_address_check(result)
            print(
                f"Saved {len(result.get('plans', []))} plans in database check {result['check_id']}."
            )
        if args.capture_text:
            print(json.dumps(result, indent=2))
    except ImportError as error:
        parser.exit(1, f"Missing dependency: {error}\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"Could not process lookup: {error}\n")
    return (
        0
        if result["status"]
        in {
            "available",
            "unavailable",
            "needs_support",
            "unit_required",
            "ambiguous_address",
            "needs_review",
            "unit_confirmation_required",
            "address_mismatch",
        }
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
