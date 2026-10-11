"""CLI entry point for Maillard."""

from __future__ import annotations

import argparse
import json
import logging
import logging.config
import sys
import time

from dotenv import load_dotenv

from maillard.__version__ import VERSION as __version__

# Load .env early so ANTHROPIC_API_KEY and other vars are in os.environ
# before any component checks them.  No-args searches CWD, works in both
# dev (project root) and Docker (WORKDIR /app).
load_dotenv()

_logger = logging.getLogger("maillard")


class _JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def _setup_logging() -> None:
    """Configure centralized logging based on MaillardConfig settings."""
    from maillard.core.config import get_config

    cfg = get_config()

    if cfg.log_format == "json":
        handler: dict[str, object] = {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stderr",
        }
        formatters: dict[str, object] = {
            "json": {"()": _JSONFormatter},
        }
    else:
        handler = {
            "class": "logging.StreamHandler",
            "formatter": "text",
            "stream": "ext://sys.stderr",
        }
        formatters = {
            "text": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
        }

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": formatters,
        "handlers": {
            "console": handler,
        },
        "root": {
            "level": cfg.log_level,
            "handlers": ["console"],
        },
        # Suppress noisy third-party loggers
        "loggers": {
            "uvicorn": {"level": "WARNING"},
            "uvicorn.access": {"level": "WARNING"},
        },
    })
    if cfg.log_file:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        try:
            Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(cfg.log_file, maxBytes=10 * 1024 * 1024,
                                               backupCount=5)
            file_handler.setFormatter(logging.getLogger().handlers[0].formatter)
            logging.getLogger().addHandler(file_handler)
        except OSError:
            logging.getLogger(__name__).warning("Cannot open log file %s; using stderr only",
                                               cfg.log_file, exc_info=True)



async def _run_sync(roast_id: str | None) -> None:
    """Push one roast or process due queued roasts."""
    from maillard.db import close_db, init_db
    from maillard.sync.site import SiteSyncClient, process_outbox_once, sync_roast_now
    client = SiteSyncClient()
    try:
        if not client.is_configured:
            print("Set MAILLARD_INGEST_SECRET to enable sync")  # noqa: T201
            return
        await init_db()
        result = await sync_roast_now(roast_id, client) if roast_id else await process_outbox_once(client)
        print(result)  # noqa: T201
    finally:
        await client.aclose()
        await close_db()


async def _run_web(host: str, port: int, driver_type: str) -> None:
    """Run the web dashboard standalone (without MCP stdio)."""
    import uvicorn

    from maillard.mcp.state import lifespan
    from maillard.web import create_app

    _setup_logging()

    async with lifespan() as state:
        state._driver_type = driver_type  # type: ignore[assignment]
        app = create_app(state)

        _logger.info("MongoDB connected")
        if state.driver is None or not state.driver.is_connected:
            _logger.warning(
                "No roaster connected — connect via dashboard or use --driver-type simulator"
            )

        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        _logger.info("Maillard dashboard: http://%s:%d", host, port)
        _logger.info("Driver type: %s", driver_type)
        await server.serve()


def main() -> int:
    """Main entry point for the Maillard CLI."""
    parser = argparse.ArgumentParser(
        prog="maillard",
        description="AI-powered coffee roasting assistant for Kaleido M10",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # MCP server command (uses stdio transport, configured via env vars)
    subparsers.add_parser("mcp", help="Run the MCP server (stdio transport)")

    # API server command
    api_parser = subparsers.add_parser("api", help="Run the REST API server")
    api_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="API server host (default: 127.0.0.1)",
    )
    api_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="API server port (default: 8080)",
    )

    # Web dashboard command (standalone, no MCP)
    web_parser = subparsers.add_parser("web", help="Run the web dashboard (standalone)")
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web server host (default: 127.0.0.1)",
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Web server port (default: 8080)",
    )
    web_parser.add_argument(
        "--driver-type",
        choices=["simulator", "kaleido"],
        default="kaleido",
        help="Roaster driver type (default: kaleido)",
    )

    sync_parser = subparsers.add_parser("sync", help="Push roasts to Example Roastery")
    sync_group = sync_parser.add_mutually_exclusive_group(required=True)
    sync_group.add_argument("--pending", action="store_true", help="Process due queued roasts")
    sync_group.add_argument("--roast", metavar="ID", help="Push a finished roast")

    args = parser.parse_args()

    if args.command == "mcp":
        from maillard.mcp import run_server

        run_server()
    elif args.command == "api":
        import uvicorn

        from maillard.api import app

        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == "web":
        import asyncio

        asyncio.run(_run_web(args.host, args.port, args.driver_type))
    elif args.command == "sync":
        import asyncio

        asyncio.run(_run_sync(args.roast))
    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
