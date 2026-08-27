from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from playwright.sync_api import expect, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _open_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def _wait_for_server(url: str, process: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(
                f"Expensetics exited with code {process.returncode}: {output}"
            )
        try:
            with urlopen(url, timeout=1):
                return
        except (URLError, TimeoutError):
            time.sleep(0.1)
    raise TimeoutError("Expensetics did not start in time")


def _assert_landscape_layout(page, viewport: dict[str, int]) -> None:
    metrics = page.evaluate(
        """() => {
            const rect = selector => {
                const element = document.querySelector(selector);
                if (!element) return null;
                const bounds = element.getBoundingClientRect();
                return {left: bounds.left, right: bounds.right};
            };
            const nav = document.querySelector('.nav-links');
            const navBounds = nav?.getBoundingClientRect();
            return {
                documentWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                brand: rect('.brand-cluster'),
                nav: rect('.nav-links'),
                actions: rect('.header-actions'),
                linksVisible: navBounds ? [...nav.querySelectorAll('a')].every(link => {
                    const bounds = link.getBoundingClientRect();
                    return bounds.left >= navBounds.left - 1 && bounds.right <= navBounds.right + 1;
                }) : false,
            };
        }"""
    )
    assert metrics["documentWidth"] <= metrics["clientWidth"] + 1
    assert metrics["brand"]["right"] <= metrics["nav"]["left"]
    assert metrics["nav"]["right"] <= metrics["actions"]["left"]
    if viewport["width"] <= 800:
        assert metrics["linksVisible"]


def _assert_dialog_fits(page, selector: str, viewport: dict[str, int]) -> None:
    bounds = page.locator(selector).bounding_box()
    assert bounds is not None
    assert bounds["x"] >= 0
    assert bounds["y"] >= 0
    assert bounds["x"] + bounds["width"] <= viewport["width"]
    assert bounds["y"] + bounds["height"] <= viewport["height"]


@pytest.mark.e2e
def test_vault_entry_autocomplete_and_primary_pages(tmp_path) -> None:
    port = _open_port()
    # WebAuthn permits local HTTP ceremonies on the localhost domain, not an
    # IP-literal RP ID; mirror the production origin in browser coverage.
    url = f"http://localhost:{port}"
    environment = {
        **os.environ,
        "EXPENSETICS_DATA_DIR": str(tmp_path / "data"),
        "EXPENSETICS_PORT": str(port),
        "EXPENSETICS_SHOW_BROWSER": "0",
    }
    # NiceGUI has a separate internal screen-test mode keyed by this pytest
    # variable; this subprocess is a normal app server controlled by Playwright.
    environment.pop("PYTEST_CURRENT_TEST", None)
    process = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_server(url, process)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            devtools = page.context.new_cdp_session(page)
            devtools.send("WebAuthn.enable")
            devtools.send(
                "WebAuthn.addVirtualAuthenticator",
                {
                    "options": {
                        "protocol": "ctap2",
                        "ctap2Version": "ctap2_1",
                        "transport": "internal",
                        "hasResidentKey": True,
                        "hasUserVerification": True,
                        "hasPrf": True,
                        "automaticPresenceSimulation": True,
                        "isUserVerified": True,
                    }
                },
            )
            page.goto(url)

            page.get_by_label("App password").fill("correct horse battery staple")
            page.get_by_label("Confirm password").fill("correct horse battery staple")
            page.get_by_role("button", name="Create encrypted vault").click()
            expect(page.get_by_role("button", name="Add expense", exact=True)).to_be_visible()

            page.get_by_role("button", name="Settings", exact=True).click()
            settings = page.get_by_role("dialog").filter(has_text="Encrypted backup")
            page.get_by_role("button", name="Set up device unlock", exact=True).click()
            expect(page.get_by_text("Make Windows Hello the default", exact=True)).to_be_visible()
            page.get_by_role("button", name="Continue to Windows Hello", exact=True).click()
            expect(page.get_by_text("Ready on this device", exact=True)).to_be_visible()
            device_record = json.loads(
                (tmp_path / "data" / "device-unlock.json").read_text(encoding="utf-8")
            )
            assert device_record["version"] == 2
            assert device_record["transports"] == ["internal"]

            page.get_by_role("button", name="Manage categories", exact=True).click()
            manager = page.get_by_role("dialog").filter(
                has_text="Customize future entry without silently changing the past."
            )
            manager.get_by_role("button", name="Add category", exact=True).click()
            add_category = page.get_by_role("dialog").filter(
                has=page.get_by_label("Category name", exact=True)
            )
            add_category.get_by_label("Category name", exact=True).fill("Pets")
            add_category.get_by_label("Subcategories", exact=True).fill(
                "Food, Veterinary"
            )
            add_category.get_by_role("button", name="Add category", exact=True).click()
            expect(manager.get_by_text("Pets", exact=True)).to_be_visible()
            expect(manager.get_by_text("Food", exact=True)).to_be_visible()
            manager.get_by_role("button", name="Close", exact=True).click()
            settings.get_by_role("button", name="Close", exact=True).click()

            page.get_by_role("button", name="Add expense", exact=True).click()
            page.get_by_placeholder("0.00").fill("17.00")
            page.get_by_placeholder("Merchant or expense").fill("Pet store")
            page.get_by_role("button", name="Pets", exact=True).click()
            page.get_by_role("button", name="More details", exact=True).click()
            page.get_by_role("combobox", name="Subcategory", exact=True).click()
            page.get_by_role("option", name="Food", exact=True).click()
            page.get_by_role("button", name="Save & close", exact=True).click()

            page.get_by_role("button", name="Settings", exact=True).click()
            page.get_by_role("button", name="Manage categories", exact=True).click()
            manager = page.get_by_role("dialog").filter(
                has_text="Customize future entry without silently changing the past."
            )
            pets = manager.locator("section.category-definition").filter(has_text="Pets")
            pets.get_by_role("button", name="Change category name", exact=True).click()
            rename = page.get_by_role("dialog").filter(
                has=page.get_by_label("New category name", exact=True)
            )
            rename.get_by_label("New category name", exact=True).fill("Pet care")
            rename.get_by_role("button", name="Save name", exact=True).click()
            page.get_by_role("button", name="Map history now", exact=True).click()
            migration = page.get_by_role("dialog").filter(
                has_text="Migrate category history"
            )
            expect(migration.get_by_text("Pet care", exact=True)).to_be_visible()
            migration.get_by_role("button", name="Migration help", exact=True).click()
            expect(page.get_by_text("What category migration means", exact=True)).to_be_visible()
            page.get_by_role("dialog").filter(
                has_text="What category migration means"
            ).get_by_role("button", name="Close", exact=True).click()
            migration.get_by_role("button", name="Preview mapping", exact=True).click()
            expect(migration.locator("section.category-migration-preview")).to_contain_text(
                "$17.00"
            )
            migration.get_by_role("button", name="Apply mapping", exact=True).click()
            confirmation = page.get_by_role("dialog").filter(
                has_text="Apply this historical mapping?"
            )
            confirmation.get_by_role("button", name="Apply mapping", exact=True).click()
            expect(
                migration.locator(".category-migration-history-row").filter(
                    has_text="Pets → Pet care"
                )
            ).to_be_visible(timeout=15_000)
            migration.get_by_role("button", name="Close", exact=True).click()
            settings.get_by_role("button", name="Close", exact=True).click()

            page.get_by_role("button", name="Add expense", exact=True).click()
            page.get_by_placeholder("0.00").fill("10.00")
            page.get_by_placeholder("Merchant or expense").fill("Costco")
            page.get_by_role("button", name="Groceries", exact=True).click()
            page.get_by_role("button", name="Save & next", exact=True).click()

            amount = page.get_by_placeholder("0.00")
            expect(amount).to_be_visible()
            expect(amount).to_have_value("")
            amount.click()
            page.keyboard.type("54.82")
            page.keyboard.press("Tab")
            page.keyboard.type("cos")
            expect(page.get_by_text("Costco", exact=True).last).to_be_visible()
            page.keyboard.press("Tab")
            page.keyboard.press("Enter")

            expect(page.get_by_placeholder("0.00")).to_be_visible()
            page.get_by_role("button", name="Close", exact=True).click()
            page.goto(f"{url}/expenses")
            expect(page.get_by_text("Costco", exact=True)).to_have_count(2)
            expect(page.get_by_text("$54.82", exact=True)).to_be_visible()

            page.get_by_role("button", name="Import CSV", exact=True).click()
            page.get_by_role("button", name="Import BMO CSV", exact=True).click()
            expect(page.get_by_text("Drop a CSV here", exact=False)).to_be_visible()
            import_csv = tmp_path / "bmo-subcategory.csv"
            import_csv.write_text(
                "Item #,Card #,Transaction Date,Posting Date,Transaction Amount,Description\n"
                "1,0000000000001001,20260802,20260803,12.34,SHELL C02146 TORONTO ON\n",
                encoding="utf-8",
            )
            page.locator("input[type=file]").set_input_files(import_csv)
            review_row = page.locator(".bank-review-row").filter(has_text="SHELL C02146")
            expect(review_row).to_be_visible()
            category = review_row.get_by_role("combobox").nth(0)
            subcategory = review_row.get_by_role("combobox").nth(1)
            expect(category).to_have_value("Transportation")
            expect(subcategory).to_have_value("Gas")
            category.click()
            page.get_by_role("option", name="Pet care", exact=True).click()
            expect(subcategory).to_have_value("Food")
            subcategory.click()
            expect(page.get_by_role("option", name="Food", exact=True)).to_be_visible()
            expect(page.get_by_role("option", name="Gas", exact=True)).to_have_count(0)
            page.get_by_role("button", name="Import selected", exact=True).click()
            expect(page.get_by_text("Recent imports", exact=True)).to_be_visible()
            recent_import = page.locator(".recent-import-row").filter(
                has_text="bmo-subcategory.csv"
            )
            expect(recent_import).to_contain_text("BMO")
            expect(recent_import).to_contain_text("Aug 2, 2026")
            expect(recent_import).to_contain_text("Imported 1 of 1 selected")

            for path, heading in (
                ("/", "Overview"),
                ("/insights", "Insights"),
                ("/budget", "Budget"),
                ("/accounts", "Accounts"),
                ("/liabilities", "Loans & liabilities"),
            ):
                page.goto(f"{url}{path}")
                expect(page.get_by_text(heading, exact=True).first).to_be_visible()

            landscape_pages = (
                ("/", "Overview"),
                ("/expenses", "Expenses"),
                ("/insights", "Insights"),
                ("/budget", "Budget"),
                ("/accounts", "Accounts"),
                ("/liabilities", "Loans & liabilities"),
            )
            for viewport in (
                {"width": 800, "height": 500},
                {"width": 1024, "height": 640},
                {"width": 1280, "height": 720},
                {"width": 1440, "height": 900},
            ):
                page.set_viewport_size(viewport)
                for path, heading in landscape_pages:
                    page.goto(f"{url}{path}")
                    expect(page.get_by_text(heading, exact=True).first).to_be_visible()
                    _assert_landscape_layout(page, viewport)

            compact_landscape = {"width": 800, "height": 500}
            page.set_viewport_size(compact_landscape)
            page.goto(f"{url}/liabilities")
            page.get_by_role("button", name="Add loan", exact=True).click()
            expect(page.get_by_role("button", name="Save", exact=True)).to_be_visible()
            _assert_dialog_fits(page, ".liability-editor-card", compact_landscape)
            page.get_by_role("button", name="Close", exact=True).click()

            page.goto(f"{url}/expenses")
            page.get_by_role("button", name="Add expense", exact=True).click()
            expect(page.get_by_role("button", name="Save & next", exact=True)).to_be_visible()
            _assert_dialog_fits(page, ".editor-card", compact_landscape)
            page.get_by_role("button", name="Close", exact=True).click()
            page.get_by_role("button", name="Import CSV", exact=True).click()
            expect(page.get_by_text("Which bank exported the transactions?", exact=True)).to_be_visible()
            _assert_dialog_fits(page, ".bank-import-card", compact_landscape)
            page.get_by_role("button", name="Close", exact=True).click()
            page.get_by_role("button", name="Settings", exact=True).click()
            expect(page.get_by_text("Encrypted backup", exact=True)).to_be_visible()
            _assert_dialog_fits(page, ".settings-card", compact_landscape)
            page.get_by_role("button", name="Close", exact=True).click()

            page.goto(url)
            page.get_by_role("button", name="Lock app", exact=True).click()
            expect(page.get_by_role("button", name="Unlock with Windows Hello")).to_be_visible()
            page.get_by_role("button", name="Unlock with Windows Hello").click()
            expect(page.get_by_role("button", name="Add expense", exact=True)).to_be_visible()
            browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
