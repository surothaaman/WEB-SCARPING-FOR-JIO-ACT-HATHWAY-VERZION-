"""Discover plan views via configured tabs, dropdowns, menus and same-site plan links.
Raw captures only: this does not infer schemas or write unvalidated plans to SQLite.
"""
import argparse
import hashlib
import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from browserbase import Browserbase
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent


def settle(page):
    # DOM text stability heuristic, bounded by 5 seconds. Not proof all remote data loaded.
    previous = None
    stable = 0
    for _ in range(20):
        page.wait_for_timeout(250)
        text = page.locator('body').inner_text(timeout=5000)
        if text == previous:
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
        previous = text


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('url')
    p.add_argument('--tab-selector', default='[role="tab"]')
    p.add_argument('--dropdown-selector', default='', help='CSS selector for plan-filter select elements only')
    p.add_argument('--menu-selector', default='', help='CSS selector for read-only navigation menu buttons only')
    p.add_argument('--link-pattern', default=r'(?i)(broadband.*plans|fiber.*plans|plans.*fiber)')
    p.add_argument('--max-pages', type=int, default=6)
    p.add_argument('--max-views', type=int, default=50)
    args = p.parse_args()
    if args.max_pages < 1 or args.max_views < 1:
        p.error('Limits must be positive')
    parts = urlsplit(args.url)
    if parts.scheme != 'https' or not parts.hostname:
        p.error('Provide an https website URL')
    load_dotenv(BASE_DIR / '.env')
    if not os.getenv('BROWSERBASE_API_KEY') or not os.getenv('BROWSERBASE_PROJECT_ID'):
        raise ValueError('Add Browserbase credentials to .env')
    output = BASE_DIR / 'captures' / ('explore-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    records, errors = [], []
    queue, seen = deque([args.url]), set()
    session = Browserbase(api_key=os.environ['BROWSERBASE_API_KEY']).sessions.create(
        project_id=os.environ['BROWSERBASE_PROJECT_ID'], timeout=900, keep_alive=False)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(session.connect_url, timeout=60000)
        try:
            page = browser.contexts[0].new_page()
            page.set_viewport_size({'width': 1440, 'height': 1000})

            def capture(label):
                if len(records) >= args.max_views:
                    return
                settle(page)
                text = page.locator('body').inner_text()
                slug = f'{len(records)+1:03d}-' + re.sub(r'[^a-zA-Z0-9_-]', '_', label)[:55]
                (output / (slug + '.html')).write_text(page.content(), encoding='utf-8')
                (output / (slug + '.txt')).write_text(text, encoding='utf-8')
                records.append({'view': label, 'url': page.url, 'file': slug, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(), 'captured_at': datetime.now(timezone.utc).isoformat()})
                print('Captured:', label, flush=True)

            while queue and len(seen) < args.max_pages and len(records) < args.max_views:
                url = queue.popleft()
                if url in seen:
                    continue
                seen.add(url)
                try:
                    response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    if not response or not response.ok:
                        raise ValueError('Unsuccessful page response')
                    if urlsplit(page.url).hostname != parts.hostname:
                        raise ValueError('Redirected to a different host')
                    # Bounded scrolling triggers lazy-loaded sections.
                    for _ in range(5):
                        page.evaluate('window.scrollBy(0, window.innerHeight)')
                        page.wait_for_timeout(200)
                    page.evaluate('window.scrollTo(0,0)')
                    capture('initial')
                    if args.menu_selector:
                        menus = page.locator(args.menu_selector)
                        for i in range(min(menus.count(), 10)):
                            menu = menus.nth(i)
                            if menu.is_visible():
                                menu.click(timeout=5000)
                                capture(f'menu-{i+1}')
                    links = page.locator('a[href]').evaluate_all('(items)=>items.map(a=>a.href)')
                    for link in links:
                        target = urlsplit(link)
                        if target.scheme == 'https' and target.hostname == parts.hostname and re.search(args.link_pattern, target.path) and link not in seen and link not in queue:
                            queue.append(link.split('#')[0])
                    tabs = page.locator(args.tab_selector)
                    names = tabs.all_text_contents()
                    for i, name in enumerate(names[:20]):
                        if len(records) >= args.max_views:
                            break
                        try:
                            tab = page.locator(args.tab_selector).nth(i)
                            if tab.is_visible():
                                tab.click(timeout=5000)
                                capture('tab-' + name.strip())
                        except Exception as e:
                            errors.append({'url': url, 'view': 'tab-'+name, 'error': str(e)})
                    # Each dropdown option is explored independently from the base page.
                    # Cross-products of multiple filters need a provider-specific adapter.
                    if args.dropdown_selector:
                        page.goto(url, wait_until='domcontentloaded')
                        settle(page)
                        count = page.locator(args.dropdown_selector).count()
                        for i in range(min(count, 5)):
                            dropdown = page.locator(args.dropdown_selector).nth(i)
                            options = dropdown.locator('option').evaluate_all('(xs)=>xs.filter(x=>!x.disabled&&x.value).map(x=>({value:x.value,label:x.textContent}))')
                            for option in options[:20]:
                                if len(records) >= args.max_views:
                                    break
                                try:
                                    page.goto(url, wait_until='domcontentloaded')
                                    page.locator(args.dropdown_selector).nth(i).select_option(option['value'], force=True, timeout=10000)
                                    capture('dropdown-' + option['label'].strip())
                                except Exception as e:
                                    errors.append({'url': url, 'view': option['label'], 'error': str(e)})
                except Exception as e:
                    errors.append({'url': url, 'error': str(e)})
        finally:
            browser.close()
            (output / 'manifest.json').write_text(json.dumps({'captures': records, 'errors': errors, 'pages_visited': sorted(seen), 'queued_urls': list(queue), 'scope': 'bounded discovery; not an exhaustive crawl or database import'}, indent=2), encoding='utf-8')
    print(f'Saved {len(records)} views; {len(errors)} errors. Folder: {output}')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
