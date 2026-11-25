import asyncio
import ast
from pathlib import Path
from datetime import datetime, timedelta
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.src.sync import SyncService


class MemoryConfig:
    def __init__(self, **state):
        self.state = {"status": "idle", "email": "test@example.invalid", **state}

    def get_config(self):
        return self.state.copy()

    def update_config(self, **values):
        self.state.update(values)

    def update_status(self, status, **values):
        self.update_config(status=status, **values)


def service(**state):
    browser = SimpleNamespace(
        start_login=AsyncMock(return_value={"status": "otp_required"}),
        submit_otp=AsyncMock(return_value={"status": "success"}),
        clear_session=AsyncMock(), cleanup=AsyncMock(), save_context=AsyncMock(),
        check_export=AsyncMock(return_value={"status": "otp_required"}),
    )
    return SyncService(browser, MemoryConfig(**state), AsyncMock())


@pytest.mark.parametrize("action", ["export", "download"])
def test_pause_authenticate_and_resume_original_job(action, tmp_path):
    async def scenario():
        sync = service()
        await sync.run(action)
        assert sync.snapshot()["status"] == "otp_needed"
        assert sync.snapshot()["pending_action"] == action
        sync.automator.cleanup.assert_not_awaited()

        # A scheduled run cannot navigate away from the code-entry page.
        await sync.run("export", scheduled=True)
        assert sync.automator.check_export.await_count == 1

        result, resume = await sync.submit_otp("123456")
        assert resume == action
        assert result["status"] in {"exporting", "downloading"}
        export = tmp_path / "export.zip"
        export.write_bytes(b"test export")
        sync.automator.check_export.return_value = {"status": "downloaded", "path": str(export)}
        await sync.run(resume)
        sync.ingest.assert_awaited_once_with(str(export))
        sync.automator.cleanup.assert_awaited_once()
        assert sync.snapshot()["status"] == "completed"
        assert sync.snapshot()["pending_action"] == ""
    asyncio.run(scenario())


def test_invalid_otp_preserves_form_browser_and_pending_job():
    async def scenario():
        sync = service(status="otp_needed", pending_action="download")
        sync.automator.submit_otp.return_value = {"status": "error", "message": "Invalid code."}
        result, action = await sync.submit_otp("000000")
        assert result["status"] == "otp_needed"
        assert result["message"] == "Invalid code."
        assert result["pending_action"] == "download"
        assert action is None
        sync.automator.cleanup.assert_not_awaited()
    asyncio.run(scenario())


def test_error_details_are_returned_and_browser_is_closed():
    async def scenario():
        sync = service()
        sync.automator.check_export.side_effect = RuntimeError("Network unavailable")
        await sync.run("download")
        assert sync.snapshot()["status"] == "error"
        assert sync.snapshot()["message"] == "Network unavailable"
        sync.automator.cleanup.assert_awaited_once()
    asyncio.run(scenario())


def test_error_result_is_not_ingested_as_a_file():
    async def scenario():
        sync = service()
        sync.automator.check_export.return_value = {"status": "error", "message": "Session expired"}
        await sync.run("download")
        sync.ingest.assert_not_awaited()
        assert sync.snapshot()["message"] == "Session expired"
    asyncio.run(scenario())


def test_restart_requests_new_login_but_remembers_paused_action():
    sync = service(status="Waiting for OTP...", pending_action="export")
    sync.reset_after_restart()
    assert sync.snapshot()["status"] == "login_needed"
    assert sync.snapshot()["pending_action"] == "export"


def test_legacy_error_status_is_normalized_without_losing_message():
    sync = service(status="Error", message="OTP required. Please sign in.")
    assert sync.snapshot()["status"] == "error"
    assert sync.snapshot()["message"] == "OTP required. Please sign in."


def test_no_concurrent_sync_or_login_while_job_is_running():
    async def scenario():
        sync = service()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def download(directory, **kwargs):
            entered.set()
            await finish.wait()
            return None
        sync.automator.check_export.side_effect = download
        task = asyncio.create_task(sync.run("download"))
        await entered.wait()
        try:
            with pytest.raises(RuntimeError, match="already running"):
                await sync.start_login("test@example.invalid")
            await sync.run("export", scheduled=True)
            assert sync.automator.check_export.await_count == 1
        finally:
            finish.set()
            await task
    asyncio.run(scenario())


def test_disabled_schedule_does_not_request_exports():
    async def scenario():
        sync = service(is_active=False)
        await sync.run("export", scheduled=True)
        sync.automator.check_export.assert_not_awaited()
    asyncio.run(scenario())


def test_authentication_routes_have_only_one_implementation():
    seen = set()
    root = Path(__file__).parents[1] / "src" / "api"
    for source in [root / "main.py", root / "routes.py"]:
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in {"post", "get"}
                        and decorator.args and isinstance(decorator.args[0], ast.Constant)):
                    key = (decorator.func.attr, decorator.args[0].value)
                    assert key not in seen, f"Duplicate route: {key}"
                    seen.add(key)


def test_login_with_an_existing_session_does_not_show_otp():
    async def scenario():
        sync = service()
        sync.automator.start_login.return_value = None
        result = await sync.start_login("test@example.invalid")
        assert result["status"] == "logged_in"
    asyncio.run(scenario())


def test_restarted_otp_job_keeps_its_action_when_requesting_new_code():
    async def scenario():
        sync = service(status="otp_needed", pending_action="download")
        result = await sync.start_login("test@example.invalid")
        sync.automator.cleanup.assert_awaited_once()
        assert result["status"] == "otp_needed"
        assert result["pending_action"] == "download"
    asyncio.run(scenario())


def test_restart_clears_stale_error_without_losing_diagnostics_or_sync_time():
    sync = service(status="Error", message="No file downloaded (timeout?)",
                   pending_action="download", last_run="2026-11-24 09:00:00")
    sync.reset_after_restart()
    assert sync.snapshot()["status"] == "idle"
    assert sync.snapshot()["message"] == "Ready to sync."
    assert sync.snapshot()["pending_action"] == ""
    assert sync.snapshot()["last_run"] == "2026-11-24 09:00:00"
    assert sync.config.get_config()["previous_sync_error"] == "No file downloaded (timeout?)"


def test_new_error_after_restart_is_still_reported():
    async def scenario():
        sync = service(status="error", message="Old timeout")
        sync.reset_after_restart()
        sync.automator.check_export.side_effect = RuntimeError("New network failure")
        await sync.run("download")
        assert sync.snapshot()["status"] == "error"
        assert sync.snapshot()["message"] == "New network failure"
    asyncio.run(scenario())


def test_pending_export_survives_restart_and_days_of_waiting():
    async def scenario():
        sync = service()
        clock = [datetime(2026, 11, 25, 11)]
        sync.now = lambda: clock[0]
        sync.automator.check_export.return_value = {"status": "pending"}
        await sync.run('export')
        assert sync.snapshot()['status'] == 'waiting_export'
        sync.automator.save_context.assert_awaited_once()
        assert sync.snapshot()['export_pending'] is True
        first_check = sync.snapshot()['last_checked_at']
        assert sync.snapshot()['data_through'] is None
        sync = SyncService(sync.automator, MemoryConfig(**sync.config.get_config()), sync.ingest,
                           now=lambda: clock[0])
        sync.reset_after_restart()
        clock[0] += timedelta(days=3)
        await sync.tick()
        assert sync.automator.check_export.call_args.kwargs['export_pending'] is True
        assert sync.snapshot()['status'] == 'waiting_export'
        assert sync.snapshot()['last_checked_at'] != first_check
        assert sync.snapshot()['data_through'] is None
        sync.ingest.assert_not_awaited()
    asyncio.run(scenario())


def test_request_is_checkpointed_before_submission_can_fail():
    async def scenario():
        sync = service()
        async def request(directory, **options):
            options['before_request']()
            assert sync.config.get_config()['export_pending'] is True
            raise RuntimeError('Connection lost after click')
        sync.automator.check_export.side_effect = request
        await sync.run('export')
        assert sync.snapshot()['export_pending'] is True
        sync.reset_after_restart()
        assert sync.snapshot()['status'] == 'waiting_export'
        sync.automator.check_export.side_effect = None
        sync.automator.check_export.return_value = {'status': 'pending'}
        await sync.tick()
        assert sync.automator.check_export.call_args.kwargs['export_pending'] is True
    asyncio.run(scenario())


def test_existing_download_is_imported_before_another_request(tmp_path):
    async def scenario():
        export = tmp_path / 'data.zip'
        export.write_bytes(b'new export')
        sync = service()
        sync.now = lambda: datetime(2026, 11, 25, 13)
        sync.automator.check_export.return_value = {'status': 'downloaded', 'path': str(export)}
        sync.ingest.return_value = {'data_through': '2026-11-23'}
        await sync.run('export')
        sync.automator.check_export.assert_awaited_once()
        assert sync.snapshot()['data_through'] == '2026-11-23'
        assert sync.snapshot()['last_checked_at'].startswith('2026-11-25')
        assert sync.snapshot()['last_imported_at'].startswith('2026-11-25')
        assert sync.snapshot()['export_pending'] is False
    asyncio.run(scenario())


def test_old_download_does_not_complete_a_pending_new_request(tmp_path):
    async def scenario():
        export = tmp_path / 'old.zip'
        export.write_bytes(b'old export')
        sync = service(export_pending=True, last_export_digest=hashlib.sha256(export.read_bytes()).hexdigest())
        sync.automator.check_export.return_value = {'status': 'downloaded', 'path': str(export)}
        await sync.run('export')
        sync.ingest.assert_not_awaited()
        sync.automator.check_export.assert_awaited_once()
        assert sync.snapshot()['export_pending'] is True
        assert sync.snapshot()['status'] == 'waiting_export'
    asyncio.run(scenario())


def test_known_download_can_trigger_one_new_request(tmp_path):
    async def scenario():
        export = tmp_path / 'old.zip'
        export.write_bytes(b'old export')
        sync = service(last_export_digest=hashlib.sha256(export.read_bytes()).hexdigest())
        sync.automator.check_export.side_effect = [
            {'status': 'downloaded', 'path': str(export)}, {'status': 'pending', 'requested': True}]
        await sync.run('export')
        assert sync.automator.check_export.await_count == 2
        assert sync.automator.check_export.call_args.kwargs['skip_download'] is True
        sync.ingest.assert_not_awaited()
        assert sync.snapshot()['status'] == 'waiting_export'
    asyncio.run(scenario())


def test_pending_check_is_not_repeated_until_its_due_time():
    async def scenario():
        sync = service(export_pending=True, status='waiting_export', next_export_check_at='2026-11-25T14:00:00')
        sync.now = lambda: datetime(2026, 11, 25, 13)
        await sync.tick()
        sync.automator.check_export.assert_not_awaited()
    asyncio.run(scenario())


def test_missed_daily_run_catches_up_once_after_wake():
    async def scenario():
        sync = service(last_schedule_day='2026-11-24', schedule_time='11:00')
        sync.now = lambda: datetime(2026, 11, 25, 14, 30)
        sync.automator.check_export.return_value = {'status':'no_export'}
        await sync.tick()
        await sync.tick()
        sync.automator.check_export.assert_awaited_once()
        assert sync.config.get_config()['last_schedule_day'] == '2026-11-25'
    asyncio.run(scenario())


def test_missed_yesterday_run_can_catch_up_before_todays_schedule():
    async def scenario():
        sync = service(last_schedule_day='2026-11-23', schedule_time='11:00')
        sync.now = lambda: datetime(2026, 11, 25, 8)
        sync.automator.check_export.return_value = {'status':'no_export'}
        await sync.tick()
        assert sync.config.get_config()['last_schedule_day'] == '2026-11-24'
    asyncio.run(scenario())


def test_new_installation_waits_for_first_scheduled_time():
    async def scenario():
        sync = service(schedule_time='11:00')
        sync.now = lambda: datetime(2026, 11, 25, 8)
        await sync.tick()
        sync.automator.check_export.assert_not_awaited()
    asyncio.run(scenario())


def test_pending_exports_still_pause_for_authentication():
    async def scenario():
        sync = service(export_pending=True, status='waiting_export')
        await sync.tick()
        assert sync.snapshot()['status'] == 'otp_needed'
        assert sync.snapshot()['export_pending'] is True
        await sync.tick()
        sync.automator.check_export.assert_awaited_once()
    asyncio.run(scenario())


def test_reimporting_older_data_does_not_move_freshness_backwards():
    sync = service(data_through='2026-11-24')
    sync.record_import({'data_through':'2026-11-20'})
    assert sync.snapshot()['data_through'] == '2026-11-24'
