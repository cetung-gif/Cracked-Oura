from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.src.api.routes import router
from backend.src.database import init_db

import asyncio
import logging
from backend.src.automation import automator
from backend.src.config import config_manager
from backend.src.sync_runtime import sync_service, refresh_freshness
import os
from pydantic import BaseModel

from contextlib import asynccontextmanager

# Configure logging
from backend.src.paths import get_user_data_dir

log_dir = get_user_data_dir()
log_file = os.path.join(log_dir, "backend_debug.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("API")
logger.info(f"API Starting... Logging to {log_file}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    refresh_freshness()
    
    sync_service.reset_after_restart()
    task = asyncio.create_task(background_worker())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await automator.cleanup()

app = FastAPI(
    title="Cracked Oura API",
    description="API for accessing Oura Ring data stored in local SQLite database.",
    version="0.1.0",
    lifespan=lifespan
)

# Configure CORS
origins = [
    "http://localhost",
    "http://localhost:3000", # Frontend
    "http://localhost:8000", # Backend
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(router)

# --- API Models for Automation ---
class AutomationConfig(BaseModel):
    email: str
    schedule_time: str
    is_active: bool
    headless: bool = True

# --- Endpoints ---

@app.get("/api/automation/status")
async def get_automation_status():
    """Returns the current automation configuration and status."""
    return sync_service.snapshot()

@app.post("/api/automation/config")
async def update_automation_config(config: AutomationConfig):
    """Updates automation settings."""
    config_manager.update_config(
        email=config.email, 
        schedule_time=config.schedule_time,
        is_active=config.is_active,
        headless=config.headless
    )
    # Configure automator with new email settings immediately
    automator.email = config.email
        
    return {"status": "success", "message": "Configuration updated."}

# Authentication, OTP submission, and manual sync routes live in routes.py.
# The scheduler uses the same service so it cannot restart an OTP challenge.
async def background_worker():
    logger.info("Background worker started.")
    while True:
        try:
            await sync_service.tick()
        except Exception:
            logger.exception("Scheduled export check failed")
        await asyncio.sleep(60)

# Mount Static Files
from fastapi.staticfiles import StaticFiles
import os

# Robustly find the frontend/dist directory relative to this file
# engine/src/api/main.py -> ../../../frontend/dist
current_dir = os.path.dirname(os.path.abspath(__file__))
dist_dir = os.path.join(current_dir, "../../../frontend/dist")

if os.path.exists(dist_dir):
    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="static")
else:
    pass


if __name__ == "__main__":
    import uvicorn
    import sys
    
    # Check if running as a PyInstaller bundle
    if getattr(sys, 'frozen', False):
        try:
            # Production (Frozen)
            uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
        except Exception as e:
            # Emergency logging if startup fails
            from backend.src.paths import get_user_data_dir
            import os
            import traceback
            
            try:
                log_path = os.path.join(get_user_data_dir(), "startup_crash.log")
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(f"Startup Crash: {e}\n")
                    f.write(traceback.format_exc())
            except:
                pass # Failed to write log
            raise e
    else:
        # Development
        # Run the server with auto-reload
        uvicorn.run("backend.src.api.main:app", host="0.0.0.0", port=8000, reload=True, reload_dirs=["backend"])
