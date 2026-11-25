from .automation import automator
from .config import config_manager
from .sync import SyncService, ingest_export, read_data_through

sync_service = SyncService(automator, config_manager, ingest_export)


def refresh_freshness():
    from .database import SessionLocal
    with SessionLocal() as db:
        config_manager.update_config(data_through=read_data_through(db) or "")
