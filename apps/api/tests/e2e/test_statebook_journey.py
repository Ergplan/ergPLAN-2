"""The monthly state rule books end to end in the browser (Playwright): an analyst generates the
month's books, opens Maharashtra's, reads the landed cost by route and the settlement, approves it
and downloads the file; without the analyst's identity the draft of another state does not open.
Run by `E2E_TESTS=tests/e2e/test_statebook_journey.py tools/dev/e2e-journey.sh`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ..engine.helpers import restore_committed

WEB = os.environ.get("E2E_WEB_URL")
API = os.environ.get("E2E_API_URL")
SHOTS = os.environ.get("E2E_SHOTS")  # a folder to keep screenshots in, when set
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not WEB or not API, reason="run through tools/dev/e2e-journey.sh"),
]


def _shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name), full_page=True)


def test_generate_review_approve_and_download() -> None:
    from playwright.sync_api import sync_playwright

    restore_committed()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{WEB}/statebooks")
        page.wait_for_selector("text=Reviewing as")
        who = page.locator('input[placeholder="name@joulewise.in"]')
        who.fill("analyst@joulewise.in")
        who.blur()
        page.locator('input[type="month"]').fill("2026-11")
        page.click('button:has-text("Generate this month\'s rule books")')
        page.wait_for_selector("text=drafted for review", timeout=120_000)
        page.wait_for_selector("h2:has-text('Maharashtra')")
        assert page.locator("text=Awaiting review").count() >= 10  # a draft per state, none approved yet
        _shot(page, "statebooks-list.png")

        row = page.locator("tr", has_text="Maharashtra State Electricity Distribution")
        row.locator("a").first.click()
        page.wait_for_selector("text=Landed cost and saving at a glance")
        text = page.locator("body").inner_text()
        for words in (
            "Third party",
            "Captive, 26% equity (group captive)",
            "What the grid costs",
            "How open-access solar and wind are settled after banking",
            "How an IEX trade is settled",
            "Banking charge, on banked units only",
            "Rules this book was computed from",
        ):
            assert words in text, words
        assert page.locator('a.ep-ref[href*="/api/documents/"]').count() >= 3  # figures open their page
        _shot(page, "statebook-review.png")

        with page.expect_download() as dl:
            page.click('button:has-text("Download file")')
        body = Path(dl.value.path()).read_text(encoding="utf-8")
        assert body.startswith("# State rule book — Maharashtra") and "status: **draft**" in body

        page.locator('input[placeholder^="What you checked"]').fill("Checked against the MERC order")
        page.click('button:has-text("Approve rule book")')
        page.wait_for_selector("text=reviewed by analyst@joulewise.in")
        assert page.locator('button:has-text("Approve rule book")').count() == 0
        _shot(page, "statebook-approved.png")
        approved = page.url

        # a visitor without the analyst's identity: the approved reference book opens, a draft does not
        page.goto(f"{WEB}/statebooks?status=draft")
        page.wait_for_selector("h2:has-text('Karnataka')")
        draft = page.locator("tr", has_text="Bangalore").locator("a").first.get_attribute("href")
        visitor = browser.new_context().new_page()
        visitor.goto(approved)
        visitor.wait_for_selector("text=Landed cost and saving at a glance")
        visitor.goto(f"{WEB}{draft}")
        visitor.wait_for_selector("text=could not be opened")
        browser.close()
