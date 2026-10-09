import os, sys, logging

# Silence low-level HTTP request logs from httpx / httpcore.
# These are used internally by both the embedder and the qdrant_client and produce
# one INFO line per HTTP call, which drowns out the actual indexing progress.
# Application-level messages (indexed successfully, errors) are unaffected.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger(__name__)

mode = os.environ.get("MODE", "watch")
_log.info(f"[entrypoint] Starting in mode={mode} ...")

try:
    if mode == "full":
        import config_loader as cfg
        import model_guard
        if not model_guard.assert_models_available(
            {"llm": cfg.llm_config()},
            api_key=cfg.omlx_api_key(),
        ):
            _log.error("[entrypoint] Required model(s) are not available. "
                       "Load the missing model(s) shown above and restart. Exiting.")
            sys.exit(2)
        from indexer import run_full_index; run_full_index()
    elif mode == "l0":
        from l0_generator import run_l0; run_l0()
    elif mode == "watch":
        from watcher import run_watcher; run_watcher()
    elif mode == "restore":
        from backup import restore_backup; restore_backup()
    elif mode == "backup":
        from backup import create_full_backup; create_full_backup()
    else:
        _log.error(f"Unknown MODE: {mode}"); sys.exit(1)
except KeyboardInterrupt:
    sys.exit(0)
