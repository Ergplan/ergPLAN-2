"""ergPLAN 2 end to end in the browser (Playwright): a factory in a state whose rules come from the
State Regulation Master (Rajasthan). The consumer gives the connection and the charges they pay,
sets targets for the five components, sees the plan drawn, and gets the optimised sizes with the
25-year view. Run by `E2E_TESTS=tests/e2e/test_ergplan2_journey.py tools/dev/e2e-journey.sh`."""

from __future__ import annotations

import os
import re

import pytest

from ..engine.helpers import restore_committed

WEB = os.environ.get("E2E_WEB_URL")
API = os.environ.get("E2E_API_URL")
SHOTS = os.environ.get("E2E_SHOTS")  # a folder to keep screenshots in, when set
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not WEB or not API, reason="run through tools/dev/e2e-journey.sh"),
]


def _field(scope, label: str):  # type: ignore[no-untyped-def]
    pattern = "^" + re.escape(label) + r"\s*\*?$"
    return (
        scope.locator(".ep-fld").filter(has=scope.page.locator("label", has_text=re.compile(pattern))).first
    )


def _shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name), full_page=True)


def test_register_state_from_targets_to_the_drawn_plan() -> None:
    from playwright.sync_api import sync_playwright

    restore_committed()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{WEB}/evaluate")
        page.wait_for_selector("text=Your factory")
        page.wait_for_timeout(1500)
        body = page.locator("body")
        _field(body, "Company").locator("input").fill("Marwar Castings")
        _field(body, "Site or town").locator("input").fill("Jodhpur works")
        _field(body, "State").locator("select").select_option("RJ")
        _field(body, "How the plant runs").locator("select").select_option("two_shift")
        _field(body, "DISCOM").locator("select").select_option("jdvvnl")
        # no approved schedule for the DISCOM: the category as the bill prints it, and the charges paid
        _field(body, "Tariff category").locator("input").fill("HT-5 Large Industrial")
        _field(body, "Supply voltage").locator("input").fill("33")
        assert page.locator(".ep-rail__missing").count() >= 1  # the energy charge is asked for
        _field(body, "Energy charge you pay").locator("input").fill("7.30")
        _field(body, "Fixed (demand) charge").locator("input").fill("405")
        _field(body, "Contract demand").locator("input").fill("3000")
        page.click('button:has-text("The year\'s units")')
        _field(body, "Units in the last twelve months").locator("input").fill("12000000")
        _field(body, "Recorded maximum demand").locator("input").fill("2700")
        # the five components are on; give targets, a cost and a CUF
        for label in (
            "Rooftop solar (behind the meter)",
            "Open-access solar, intra-state",
            "Open-access wind, intra-state",
            "Battery behind the meter",
            "Battery at the open-access plant",
        ):
            t = page.locator(f'button[role=switch][aria-label="{label}"]')
            assert t.get_attribute("aria-checked") == "true", label
        solar = page.locator(".ep-tech").filter(has_text="Open-access solar, intra-state").first
        _field(solar, "Target size").locator("input").fill("6")
        _field(solar, "CUF").locator("input").fill("24")
        roof = page.locator(".ep-tech").filter(has_text="Rooftop solar (behind the meter)").first
        _field(roof, "Target size").locator("input").fill("1")
        _field(roof, "Capital cost").locator("input").fill("36000")
        # the plan as set, drawn
        iso = page.locator(".ep-iso").first
        iso.scroll_into_view_if_needed()
        assert "target 6 MW" in iso.inner_text() and "Battery at the plant" in iso.inner_text()
        _shot(page, "ergplan2-inputs.png")
        assert page.locator(".ep-rail__missing").count() == 0
        page.click('.ep-ev__footer button:has-text("Get results")')
        page.wait_for_url(re.compile(r"/results\?c="), timeout=300_000)
        page.wait_for_selector('h2:has-text("25-year view"), .ep-error', timeout=900_000)
        assert page.locator(".ep-error").count() == 0, page.locator(".ep-error").first.text_content()
        drawn = page.locator("#system .ep-iso").inner_text()
        assert "Open-access solar" in drawn and "MW" in drawn and "target 6 MW" in drawn
        for heading in (
            "Summary",
            "Recommended system",
            "Cost comparison, year one",
            "25-year view",
            "Dispatch",
        ):
            assert page.locator(f'h2:has-text("{heading}")').count() >= 1, heading
        # the state's rules are shown as register entries, the tariff as the consumer's own
        assert page.locator('.eg-chip:has-text("register")').count() > 0
        assert page.locator('.eg-chip:has-text("you said")').count() > 0
        _shot(page, "ergplan2-results.png")
        browser.close()
