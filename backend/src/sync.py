"""One authentication and job lifecycle for manual and scheduled syncs."""
import asyncio
from datetime import datetime
import tempfile


BUSY_STATUSES = {"exporting", "downloading", "initializing"}


class SyncService:
    def __init__(self, automator, config, ingest):
        self.automator = automator
        self.config = config
        self.ingest = ingest
        self._lock = None

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
        elif status not in {"idle", "login_needed", "otp_needed", "logged_in", "exporting", "downloading", "completed", "error", "initializing"}:
            status = "initializing" if self._lock and self._lock.locked() else "idle"
        message = cfg.get("message") or raw
        return {"status": status, "message": message,
                "pending_action": cfg.get("pending_action"),
                "last_run": cfg.get("last_run"), "next_run": cfg.get("next_run")}

    def set_status(self, status, message, **kwargs):
        self.config.update_status(status, message=message, **kwargs)

    def reset_after_restart(self):
        state = self.snapshot()
        if state["status"] == "otp_needed":
            self.set_status("login_needed", "Sign in again to request a fresh code; the app was restarted.")
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

    async def run(self, action, scheduled=False):
        if self.lock.locked():
            return
        if scheduled and (not self.config.get_config().get("is_active", True)
                          or self.snapshot()["status"] in {"otp_needed", "login_needed"} | BUSY_STATUSES):
            return
        async with self.lock:
            waiting_for_otp = False
            self.set_status("exporting" if action == "export" else "downloading",
                            "Requesting and waiting for an export…" if action == "export" else "Checking for a downloadable export…",
                            pending_action=action)
            try:
                cfg = self.config.get_config()
                self.automator.email = cfg.get("email", "")
                with tempfile.TemporaryDirectory() as directory:
                    if action == "export":
                        result = await self.automator.request_new_export_and_download(directory)
                    else:
                        result = await self.automator.download_existing_export(directory)
                    if isinstance(result, dict):
                        if result.get("status") == "otp_required":
                            waiting_for_otp = True
                            self.set_status("otp_needed", "Sync paused. Enter the code sent to your email.", pending_action=action)
                            return
                        raise RuntimeError(result.get("message", "Sync failed."))
                    if not result:
                        raise RuntimeError("No downloadable export was found. Check whether Oura is still preparing your export, then retry.")
                    self.set_status("downloading", "Importing the downloaded export…")
                    await self.ingest(result)
                self.set_status("completed", "Sync and import complete.", pending_action="",
                                last_run=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            except Exception as exc:
                self.set_status("error", str(exc), pending_action="")
            finally:
                # The code is tied to this browser page. Keep it alive until
                # authentication completes; scheduled retries must not touch it.
                if not waiting_for_otp:
                    await self.automator.cleanup()


async def ingest_export(path):
    from .database import SessionLocal
    from .ingestion import OuraParser
    db = SessionLocal()
    try:
        OuraParser(db).parse_zip(path)
    finally:
        db.close()

