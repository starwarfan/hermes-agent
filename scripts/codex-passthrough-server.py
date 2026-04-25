#!/usr/bin/env python3
"""Minimal raw passthrough server for Codex CLI."""

from __future__ import annotations

import logging
import os
import signal
from aiohttp import web

from gateway.platforms.codex_passthrough import (
    DEFAULT_UPSTREAM_BASE,
    create_codex_passthrough_app,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else default


def create_app() -> web.Application:
    return create_codex_passthrough_app()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("CODEX_PROXY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    host = _env("CODEX_PROXY_HOST", DEFAULT_HOST)
    port = int(_env("CODEX_PROXY_PORT", str(DEFAULT_PORT)))
    app = create_app()
    logging.info("Codex passthrough listening on http://%s:%d/v1", host, port)
    logging.info("Upstream base: %s", _env("CODEX_UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE).rstrip("/"))
    web.run_app(app, host=host, port=port, handle_signals=signal.getsignal(signal.SIGINT) is not None)


if __name__ == "__main__":
    main()
