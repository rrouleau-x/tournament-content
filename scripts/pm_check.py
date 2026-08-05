#!/usr/bin/env python3
"""Final sweep: notifications, team calendar events, roster counts."""
import json, time
from playwright.sync_api import sync_playwright

CREDS = json.load(open("/Users/rrouleau/.hermes/playmetrics_creds.json"))

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.goto("https://playmetrics.com/login", timeout=60000)
        time.sleep(2)
        page.fill('input[type="email"], input[name="email"]', CREDS["email"])
        page.fill('input[type="password"]', CREDS["password"])
        page.click('button:has-text("Login")')
        time.sleep(8)

        body = page.inner_text("body")
        if "Team Staff" in body[:200]:
            sel = page.query_selector('.role-selector, [class*="role-selector"]')
            if sel:
                sel.click()
                time.sleep(1)
            page.evaluate("""
                (() => {
                    const items = document.querySelectorAll('a.dropdown-item.role-selector__item');
                    for (const a of items) {
                        if (a.textContent.includes('Player Contact') && a.textContent.includes('Savannah United')) {
                            a.click(); break;
                        }
                    }
                })()
            """)
            time.sleep(3)

        # 1. Notifications panel
        page.evaluate("""(() => { const el = document.querySelector('a[aria-label="view notifications"]'); if (el) el.click(); })()""")
        time.sleep(4)
        full = page.evaluate("document.body.innerText")
        print("=== NOTIFICATIONS PANEL ===")
        # print the panel region
        idx = full.find('Notifications')
        if idx >= 0:
            print(full[idx:idx+2500])
        else:
            print(full[:1500])

        # close panel (click EXIT or elsewhere)
        page.evaluate("""(() => { const els = Array.from(document.querySelectorAll('button, a')); const b = els.find(e => (e.textContent||'').trim() === 'EXIT'); if (b) b.click(); })()""")
        time.sleep(2)

        # 2. Team calendar - click Calendar link
        page.evaluate("""
            (() => {
                const links = Array.from(document.querySelectorAll('a'));
                const cal = links.find(a => (a.textContent||'').trim() === 'Calendar');
                if (cal) cal.click();
            })()
        """)
        time.sleep(5)
        full = page.evaluate("document.body.innerText")
        print("=== TEAM CALENDAR (first 3000) ===")
        print(full[:3000])

        # 3. Roster counts — navigate to team page then Roster tab
        page.evaluate("""
            (() => {
                const links = Array.from(document.querySelectorAll('a'));
                const team = links.find(a => (a.textContent||'').includes('17/18B Travel'));
                if (team) team.click();
            })()
        """)
        time.sleep(4)
        # click Roster sub-tab
        page.evaluate("""
            (() => {
                const links = Array.from(document.querySelectorAll('a'));
                const tab = links.find(a => (a.textContent||'').trim() === 'Roster');
                if (tab) tab.click();
            })()
        """)
        time.sleep(5)
        full = page.evaluate("document.body.innerText")
        print("=== ROSTER PAGE (first 2000) ===")
        print(full[:2000])
        # count players
        import re
        m = re.search(r'TOTAL PLAYERS:\s*(\d+)', full)
        print("TOTAL PLAYERS MATCH:", m.group(0) if m else "not found")

        browser.close()

if __name__ == "__main__":
    main()