"""Browser regressions for Oura's passkey/email-code selection screen.

Run from the repository root:
    python -m playwright install chromium
    python -m pytest backend/tests
"""

import asyncio
import importlib
import os
from unittest.mock import patch

import pytest
from playwright.async_api import async_playwright

from backend.src import paths


@pytest.fixture
def automator_class(monkeypatch, tmp_path):
    # Importing config creates files; keep tests away from real account data.
    monkeypatch.setattr(paths, "get_user_data_dir", lambda: tmp_path)
    # The module's singleton changes this variable; retain the test browser path.
    with patch.dict(os.environ):
        return importlib.import_module("backend.src.automation").OuraAutomator


PASSKEY = '<button name="selectedId" id="passkey-button">Use passkey</button>'
EMAIL = '<button name="selectedId" id="email-button">Email me a code</button>'


@pytest.mark.parametrize(
    "buttons",
    [
        PASSKEY + EMAIL,
        EMAIL + PASSKEY,
        EMAIL,
        '<button name="selectedId" id="email-button">Send code</button>',
    ],
    ids=["passkey-first", "email-first", "email-only", "legacy-send-code"],
)
def test_selects_email_code(automator_class, buttons):
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(
                    buttons
                    + """
                    <script>
                    window.clicked = [];
                    document.querySelectorAll('button').forEach(button => {
                        button.onclick = () => {
                            window.clicked.push(button.id);
                            if (button.id === 'email-button') {
                                const input = document.createElement('input');
                                input.name = 'otp';
                                document.body.append(input);
                            }
                        };
                    });
                    </script>
                    """
                )
                automator = automator_class.__new__(automator_class)
                automator.page = page

                result = await automator._check_otp_screen()

                assert result["status"] == "otp_required"
                assert await page.evaluate("window.clicked") == ["email-button"]
            finally:
                await browser.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("otp_input", ['name="otp"', 'id="otp-code"'])
def test_existing_otp_does_not_request_another_code(automator_class, otp_input):
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(
                    PASSKEY + EMAIL + f"<input {otp_input}>"
                    + """<script>
                    window.clicked = false;
                    document.querySelectorAll('button').forEach(button => {
                        button.onclick = () => { window.clicked = true; };
                    });
                    </script>"""
                )
                automator = automator_class.__new__(automator_class)
                automator.page = page

                result = await automator._check_otp_screen()

                assert result["status"] == "otp_required"
                assert await page.evaluate("window.clicked") is False
            finally:
                await browser.close()

    asyncio.run(scenario())


def test_passkey_only_screen_is_not_clicked(automator_class):
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(
                    PASSKEY + """<script>
                    window.clicked = false;
                    document.querySelector('button').onclick = () => {
                        window.clicked = true;
                    };
                    </script>"""
                )
                automator = automator_class.__new__(automator_class)
                automator.page = page

                assert await automator._check_otp_screen() is None
                assert await page.evaluate("window.clicked") is False
            finally:
                await browser.close()

    asyncio.run(scenario())
