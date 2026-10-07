"""Render a page in a real (stealth) Chromium to get past Cloudflare challenges.
Run under xvfb-run: python browser_fetch.py URL OUT.html"""
import sys, time
from patchright.sync_api import sync_playwright

url, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch(headless=False)
    pg = b.new_page()
    pg.goto(url, wait_until="domcontentloaded", timeout=45000)
    for _ in range(20):
        time.sleep(2)
        t = pg.title().lower()
        if "just a moment" not in t and "attention required" not in t:
            break
    else:
        sys.exit("still blocked by the site challenge")
    time.sleep(1)
    open(out, "w", encoding="utf-8").write(pg.content())
    b.close()
