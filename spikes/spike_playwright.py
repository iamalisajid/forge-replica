"""Spike: aria_snapshot(mode="ai") refs in Playwright 1.63, and resolving refs to portable role locators."""
import asyncio
import inspect
import re

from playwright.async_api import async_playwright

PAGE = """<!doctype html><html><body>
<header><nav aria-label="Main"><a href="#p">Projects</a></nav></header>
<main>
  <h1>Issues</h1>
  <button>New issue</button>
  <table><tbody>
    <tr><td>Alpha bug</td><td><button>Delete</button></td></tr>
    <tr><td>Beta bug</td><td><button>Delete</button></td></tr>
  </tbody></table>
  <dialog open aria-label="New issue"><label>Title <input name="title"></label>
    <select aria-label="Status"><option>open</option><option>closed</option></select>
    <button>Create</button></dialog>
</main></body></html>"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(PAGE)
        print("aria_snapshot signature:", inspect.signature(page.locator("body").aria_snapshot))
        has_page_level = hasattr(page, "aria_snapshot")
        print("page.aria_snapshot exists:", has_page_level)
        try:
            snap = await (page.aria_snapshot(mode="ai") if has_page_level else page.locator("body").aria_snapshot(mode="ai"))
        except TypeError as e:
            print("mode='ai' not supported:", e)
            snap = await page.locator("body").aria_snapshot()
        print("---- snapshot ----\n" + snap)
        refs = re.findall(r"\[ref=(e\d+)\]", snap)
        print("refs found:", len(refs))
        # Resolve the second "Delete" button by ref and click it.
        lines = [l for l in snap.splitlines() if "Delete" in l and "[ref=" in l]
        print("delete lines:", lines)
        if lines:
            ref = re.search(r"\[ref=(e\d+)\]", lines[-1]).group(1)
            loc = page.locator(f"aria-ref={ref}")
            print("aria-ref count:", await loc.count(), "text:", await loc.inner_text())
            # Portable locator: role+name is ambiguous for duplicates -> scope within the row.
            ambiguous = await page.get_by_role("button", name="Delete", exact=True).count()
            scoped = page.get_by_role("row").filter(has_text="Beta bug").get_by_role("button", name="Delete", exact=True)
            print("role+name count:", ambiguous, "| scoped count:", await scoped.count())
        await browser.close()
        print("PASS" if refs else "FAIL: no refs")


asyncio.run(main())
