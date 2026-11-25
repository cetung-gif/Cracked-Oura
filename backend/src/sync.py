"""One authentication and job lifecycle for manual and scheduled syncs."""
import asyncio
from datetime import datetime, timedelta
import tempfile
import hashlib
from pathlib import Path


BUSY_STATUSES = {"exporting", "downloading", "initializing"}


class SyncService:
    def __init__(self, automator, config, ingest, now=None):
        self.automator = automator
        self.config = config
        self.ingest = ingest
        self._lock = None
        self.now = now or datetime.now

    @property
    def lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def snapshot(self):
        cfg = self.config.get_config()
        raw = cfg.get("status", "idle")
        status = raw.lower().strip().rstrip(".")
        # Accept statuses written by the previous app until the next transition.
        if "otp" in status and "error" not in status:
            status = "otp_needed"
        elif status.startswith("error") or status in {"failed to download export", "ingestion failed"}:
            status = "error"
        elif status == "processing":
            status = "exporting"
        elif status not in {"idle", "login_needed", "otp_needed", "logged_in", "exporting", "downloading", "completed", "error", "initializing", "waiting_export"}:
            status = "initializing" if self._lock and self._lock.locked() else "idle"
        message = cfg.get("message") or raw
        return {"status": status, "message": message,
                "pending_action": cfg.get("pending_action"),
                "last_run": cfg.get("last_run"), "next_run": cfg.get("next_run"),
                **{key: cfg.get(key) for key in (
                    "export_pending", "export_requested_at", "next_export_check_at",
                    "last_checked_at", "last_imported_at", "data_through"
                )}}

    def set_status(self, status, message, **kwargs):
        self.config.update_status(status, message=message, **kwargs)

    def reset_after_restart(self):
        state = self.snapshot()
        if state["status"] == "otp_needed":
            self.set_status("login_needed", "Sign in again to request a fresh code; the app was restarted.")
        elif self.config.get_config().get("export_pending"):
            self.set_status("waiting_export", "Waiting for Oura to prepare your export. Checks resume automatically.",
                            next_export_check_at=self.now().isoformat(), pending_action="")
        elif state["status"] in BUSY_STATUSES:
            self.set_status("idle", "The previous job was interrupted. You can retry it.", pending_action="")
        elif state["status"] == "error":
            # A failure from a previous process is history, not a new failure
            # caused by opening the app. Retain the detail for diagnostics.
            self.set_status("idle", "Ready to sync.", pending_action="",
                            previous_sync_error=state["message"])

    def require_available(self):
        if self.lock.locked() or self.snapshot()["status"] in BUSY_STATUSES:
            raise RuntimeError("A sync operation is already running.")

    def prepare(self, action):
        self.require_available()
        if self.snapshot()["status"] in {"otp_needed", "login_needed"}:
            raise RuntimeError("Finish signing in before starting another sync.")
        self.set_status("exporting" if action == "export" else "downloading",
                        "Starting sync…", pending_action=action)

    async def start_login(self, email):
        self.require_available()
        async with self.lock:
            self.config.update_config(email=email)
            try:
                if self.snapshot()["status"] == "otp_needed":
                    await self.automator.cleanup()
                result = await self.automator.start_login(email)
                if result and result.get("status") == "otp_required":
                    self.set_status("otp_needed", "Enter the code sent to your email.")
                else:
                    self.set_status("logged_in", "Signed in. Your session has been saved.")
                return self.snapshot()
            except Exception as exc:
                self.set_status("error", str(exc))
                return self.snapshot()

    async def submit_otp(self, otp):
        self.require_available()
        async with self.lock:
            if self.snapshot()["status"] != "otp_needed":
                self.set_status("login_needed", "Start login to request a fresh code.")
                return self.snapshot(), None
            try:
                result = await self.automator.submit_otp(otp)
            except Exception as exc:
                result = {"status": "error", "message": str(exc)}
            if result.get("status") != "success":
                # Keep the form and paused job available for another attempt.
                self.set_status("otp_needed", result.get("message", "The code was not accepted. Try again."))
                return self.snapshot(), None
            action = self.config.get_config().get("pending_action")
            self.set_status("logged_in", "Signed in. Your session has been saved.", pending_action="")
            if action in {"export", "download"}:
                self.set_status("exporting" if action == "export" else "downloading",
                                "Signed in. Resuming sync…", pending_action=action)
                return self.snapshot(), action
            return self.snapshot(), None

    async def clear_session(self):
        self.require_available()
        async with self.lock:
            await self.automator.clear_session()
            self.set_status("idle", "Session cleared. Sign in to sync again.", pending_action="")
            return self.snapshot()

    def mark_waiting(self):
        self.set_status("waiting_export", "Oura is preparing your export. It will be checked again automatically.",
                        export_pending=True, pending_action="",
                        next_export_check_at=(self.now() + timedelta(hours=1)).isoformat(), retry_action="export")

    def record_import(self, metadata, digest=None):
        values = {"last_imported_at": self.now().isoformat()}
        if isinstance(metadata, dict) and metadata.get("data_through"):
            existing = self.config.get_config().get("data_through")
            values["data_through"] = max(existing or "", metadata["data_through"])
        if digest:
            values["last_export_digest"] = digest
        self.config.update_config(**values)

    async def tick(self):
        """Resume due checks and catch up one missed daily run after wake/startup."""
        cfg = self.config.get_config()
        if not cfg.get("is_active", True) or not cfg.get("email") or self.lock.locked():
            return
        if self.snapshot()["status"] in {"otp_needed", "login_needed"} | BUSY_STATUSES:
            return
        now = self.now()
        hour, minute = map(int, cfg.get("schedule_time", "11:00").split(":"))
        today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        daily_due = today if now >= today else today - timedelta(days=1)
        self.config.update_config(next_run=(today if now < today else today + timedelta(days=1)).isoformat())
        next_check = cfg.get("next_export_check_at")
        if cfg.get("export_pending") or next_check:
            if not next_check or now >= datetime.fromisoformat(next_check):
                await self.run(cfg.get("retry_action") or "export", scheduled=True)
            return
        last_day = cfg.get("last_schedule_day")
        # A new installation starts at today's chosen time, rather than treating
        # yesterday as a missed run before the first scheduled time has arrived.
        if last_day is None and now < today:
            return
        if not last_day or last_day < daily_due.date().isoformat():
            self.config.update_config(last_schedule_day=daily_due.date().isoformat())
            await self.run("export", scheduled=True)

    async def run(self, action, scheduled=False):
        if self.lock.locked():
            return
        if scheduled and (not self.config.get_config().get("is_active", True)
                          or self.snapshot()["status"] in {"otp_needed", "login_needed"} | BUSY_STATUSES):
            return
        async with self.lock:
            waiting_for_otp = False
            self.set_status("exporting" if action == "export" else "downloading",
                            "Checking Oura's export status…", pending_action=action)
            try:
                cfg = self.config.get_config()
                self.automator.email = cfg.get("email", "")
                def checkpoint_request():
                    # Write BEFORE clicking. A crash during submission must not
                    # create another request on the next launch.
                    self.config.update_config(export_pending=True, export_requested_at=self.now().isoformat(),
                                              next_export_check_at=self.now().isoformat())

                with tempfile.TemporaryDirectory() as directory:
                    result = await self.automator.check_export(
                        directory, request_if_missing=action == "export",
                        export_pending=bool(cfg.get("export_pending")), before_request=checkpoint_request)
                    if result.get("status") == "otp_required":
                        waiting_for_otp = True
                        self.set_status("otp_needed", "Sync paused. Enter the code sent to your email.", pending_action=action)
                        return
                    if result.get("status") in {"pending", "no_export", "downloaded"}:
                        await self.automator.save_context()
                        self.config.update_config(last_checked_at=self.now().isoformat())
                        # A successful manual check after today's scheduled
                        # time also satisfies that day's check.
                        hour, minute = map(int, cfg.get("schedule_time", "11:00").split(":"))
                        now = self.now()
                        if now >= now.replace(hour=hour, minute=minute, second=0, microsecond=0):
                            self.config.update_config(last_schedule_day=now.date().isoformat())
                    if result.get("status") == "pending":
                        self.mark_waiting()
                        if result.get("request_unconfirmed"):
                            self.config.update_config(message="Oura has not confirmed the request yet. Check its export page; the app will check again without submitting a duplicate.")
                        return
                    if result.get("status") == "no_export":
                        self.set_status("idle", "No export is available yet. Use Request New to prepare one.",
                                        next_export_check_at="", pending_action="", retry_action="")
                        return
                    if result.get("status") != "downloaded" or not result.get("path"):
                        raise RuntimeError(result.get("message", "Could not check the Oura export."))
                    path = result["path"]
                    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                    duplicate = digest == self.config.get_config().get("last_export_digest")
                    if duplicate and cfg.get("export_pending"):
                        # An older download can remain visible while Oura
                        # prepares the requested snapshot. It isn't completion.
                        self.mark_waiting()
                        return
                    if not duplicate:
                        self.set_status("downloading", "Importing the downloaded export…")
                        metadata = await self.ingest(path)
                        self.record_import(metadata, digest)
                    self.config.update_config(export_pending=False, export_requested_at="",
                                              next_export_check_at="", retry_action="")
                    if duplicate and action == "export" and not cfg.get("export_pending"):
                        # We checked/imported existing downloads first. Request
                        # another snapshot only when the existing ZIP is known.
                        result = await self.automator.check_export(
                            directory, request_if_missing=True, skip_download=True,
                            export_pending=False, before_request=checkpoint_request)
                        if result.get("status") == "otp_required":
                            waiting_for_otp = True
                            self.set_status("otp_needed", "Enter your email code to continue.", pending_action=action)
                            return
                        if result.get("status") == "pending":
                            await self.automator.save_context()
                            self.mark_waiting()
                            if result.get("request_unconfirmed"):
                                self.config.update_config(message="Oura has not confirmed the request yet. Check its export page; the app will check again without submitting a duplicate.")
                            return
                        if result.get("status") != "no_export":
                            raise RuntimeError(result.get("message", "Could not request a fresh export."))
                self.set_status("completed", "The available export is already imported." if duplicate else "Sync and import complete.",
                                pending_action="", last_run=self.now().isoformat())
            except Exception as exc:
                self.set_status("error", str(exc), pending_action="", retry_action=action,
                                next_export_check_at=(self.now() + timedelta(hours=1)).isoformat())
            finally:
                if not waiting_for_otp:
                    await self.automator.cleanup()


def read_data_through(db):
    """Newest stored measurement day, independent of when an export was checked."""
    from sqlalchemy import func, select
    from .models import Base
    days = []
    for table in Base.metadata.sorted_tables:
        if "day" in table.c:
            value = db.scalar(select(func.max(table.c.day)).where(table.c.day <= datetime.now().date()))
            if value:
                days.append(value.isoformat())
        elif table.name in {"heart_rate", "temperature"}:
            value = db.scalar(select(func.max(table.c.timestamp)).where(table.c.timestamp <= datetime.now()))
            if value:
                days.append(value.date().isoformat())
    return max(days) if days else None


async def ingest_export(path):
    from .database import SessionLocal
    from .ingestion import OuraParser
    db = SessionLocal()
    try:
        OuraParser(db).parse_zip(path)
        return {"data_through": read_data_through(db)}
    finally:
        db.close()
