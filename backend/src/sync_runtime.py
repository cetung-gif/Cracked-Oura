from .automation import automator
from .config import config_manager
from .sync import SyncService, ingest_export

sync_service = SyncService(automator, config_manager, ingest_export)
