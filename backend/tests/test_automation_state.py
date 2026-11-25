import asyncio
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from backend.src import paths


@pytest.fixture
def automation(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "get_user_data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ):
        module = importlib.import_module("backend.src.automation")
        yield module.OuraAutomator


def test_session_migrates_once_and_is_not_resurrected_after_clear(automation, tmp_path):
    legacy = tmp_path / "oura_session.json"
    legacy.write_text(json.dumps({"cookies": [], "origins": []}))
    # The module singleton may already have checked migration; this test models
    # first launch of a new installation containing a legacy session.
    marker = tmp_path / "data" / ".session-migrated"
    marker.unlink(missing_ok=True)
    automator = automation()
    saved = Path(automator.storage_state_path)
    assert saved.parent == tmp_path / "data"
    assert saved.read_text() == legacy.read_text()
    assert saved.stat().st_mode & 0o777 == 0o600
    asyncio.run(automator.clear_session())
    assert not saved.exists()
    automation()
    assert not saved.exists()


@pytest.mark.parametrize("url,logged_in", [
    ("https://membership.ouraring.com/data-export", True),
    ("https://membership.ouraring.com/", True),
    ("https://membership.ouraring.com/login", False),
    ("https://membership.ouraring.com.evil.invalid/data-export", False),
])
def test_authenticated_export_page_reuses_session(automation, url, logged_in):
    automator = automation()
    automator.page = SimpleNamespace(url=url)
    assert automator._is_logged_in() is logged_in


def test_existing_otp_challenge_is_not_restarted(automation):
    async def scenario():
        automator = automation()
        automator.email = "test@example.invalid"
        automator.page = SimpleNamespace(url="https://membership.ouraring.com/login", wait_for_load_state=AsyncMock())
        automator._check_otp_screen = AsyncMock(return_value={"status": "otp_required"})
        automator._perform_login_actions = AsyncMock()
        result = await automator.login()
        assert result["status"] == "otp_required"
        automator._perform_login_actions.assert_not_awaited()
    asyncio.run(scenario())


def test_session_save_is_private(automation):
    async def scenario():
        automator = automation()
        async def save(path):
            Path(path).write_text('{"cookies": [], "origins": []}')
        automator.context = SimpleNamespace(storage_state=AsyncMock(side_effect=save))
        await automator.save_context()
        assert Path(automator.storage_state_path).stat().st_mode & 0o777 == 0o600
    asyncio.run(scenario())
