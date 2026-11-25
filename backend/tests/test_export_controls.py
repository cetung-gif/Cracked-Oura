import asyncio
import importlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
from playwright.async_api import async_playwright
from backend.src import paths


@pytest.fixture
def automator_class(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, 'get_user_data_dir', lambda: tmp_path)
    with patch.dict(os.environ):
        return importlib.import_module('backend.src.automation').OuraAutomator


@pytest.mark.parametrize('html,request_new,pending,expected', [
    ('<button>Request your data</button>', False, False, 'no_export'),
    ('<button>Request your data</button>', True, False, 'pending'),
    ('<button>Request your data</button>', True, True, 'pending'),
    ('<span>Pending</span><button disabled>Request your data</button>', True, False, 'pending'),
    ('<button disabled>Request your data</button>', True, False, 'pending'),
    ('<span>Pending</span><button aria-label="Download data">Old ZIP</button>', True, True, 'pending'),
])
def test_export_state_and_request_dedup(automator_class, tmp_path, html, request_new, pending, expected):
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content('<main><button>Unrelated action</button>'+html+'</main><script>window.clicked=[]; document.querySelectorAll("button").forEach(b=>b.onclick=()=>{window.clicked.push(b.textContent); if(b.textContent === "Request your data") b.disabled=true;});</script>')
                automator = automator_class.__new__(automator_class)
                automator._is_initialized = True
                automator.page = page
                automator._navigate_to_export_page = AsyncMock(return_value=True)
                checkpoint = []
                result = await automator.check_export(str(tmp_path), request_if_missing=request_new,
                    export_pending=pending, before_request=lambda: checkpoint.append(True))
                assert result['status'] == expected
                clicked = await page.evaluate('window.clicked')
                should_click = request_new and not pending and html == '<button>Request your data</button>'
                assert clicked == (['Request your data'] if should_click else [])
                assert checkpoint == ([True] if should_click else [])
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_unknown_page_is_not_reported_as_pending(automator_class, tmp_path):
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content('<main><button>Delete account</button></main>')
                automator = automator_class.__new__(automator_class)
                automator._is_initialized = True
                automator.page = page
                automator._navigate_to_export_page = AsyncMock(return_value=True)
                with pytest.raises(RuntimeError, match='identify'):
                    await automator.check_export(str(tmp_path), export_pending=True, request_if_missing=True)
            finally:
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('control', ['<button aria-label="Download data">Download</button>', '<a href="#">Download export</a>'])
def test_actual_download_control_is_required(automator_class, tmp_path, control):
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content('<main><button>Request your data</button>'+control+'</main>')
                automator = automator_class.__new__(automator_class)
                automator._is_initialized = True
                automator.page = page
                automator._navigate_to_export_page = AsyncMock(return_value=True)
                automator._download_file = AsyncMock(return_value=str(tmp_path/'data.zip'))
                result = await automator.check_export(str(tmp_path), request_if_missing=True)
                assert result['status'] == 'downloaded'
                automator._download_file.assert_awaited_once()
            finally:
                await browser.close()
    asyncio.run(scenario())
