#!/usr/bin/env python3
"""Minimal raw passthrough server for Codex CLI.

This server is intentionally separate from the Hermes gateway and agent stack.
It validates a local bearer token, replaces it with the Codex/ChatGPT access
token, forwards requests to the Codex backend, and streams bytes back unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Iterable

from aiohttp import ClientSession, ClientTimeout, web


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_PROXY_KEY = "replace-with-your-own-key"
DEFAULT_UPSTREAM_BASE = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_STRIP_HEADERS = HOP_BY_HOP_HEADERS | {
    "host",
    "content-length",
    # Avoid zstd/content decoding issues in simple proxy environments.
    "accept-encoding",
}
RESPONSE_STRIP_HEADERS = HOP_BY_HOP_HEADERS | {
    "content-length",
    "content-encoding",
}


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else default


def _bearer_from_header(header_value: str | None) -> str:
    if not header_value:
        return ""
    prefix = "bearer "
    if not header_value.lower().startswith(prefix):
        return ""
    return header_value[len(prefix):].strip()


def _load_upstream_token() -> str:
    env_token = os.getenv("CODEX_UPSTREAM_BEARER", "").strip()
    if env_token:
        return env_token.removeprefix("Bearer ").strip()

    auth_path = Path(_env("CODEX_AUTH_PATH", str(DEFAULT_CODEX_AUTH_PATH))).expanduser()
    with auth_path.open("r", encoding="utf-8") as f:
        auth = json.load(f)

    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError(f"No tokens.access_token found in {auth_path}")
    return access_token.strip()


def _copy_headers(headers: Iterable[tuple[str, str]], *, strip: set[str]) -> dict[str, str]:
    copied: dict[str, str] = {}
    for name, value in headers:
        if name.lower() in strip:
            continue
        copied[name] = value
    return copied
def _upstream_url(request: web.Request) -> str:
    upstream_base = _env("CODEX_UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE).rstrip("/")
    path = request.path
    if path.startswith("/v1/"):
        path = path[3:]
    elif path == "/v1":
        path = ""
    return f"{upstream_base}{path}"


class CodexPassthrough:
    def __init__(self) -> None:
        self.proxy_key = _env("CODEX_PROXY_KEY", DEFAULT_PROXY_KEY)
        self.model_name = _env("CODEX_PROXY_MODEL", "gpt-5.4")
        self._session: ClientSession | None = None

    async def start(self, app: web.Application) -> None:
        timeout = ClientTimeout(total=None, sock_connect=30, sock_read=None)
        self._session = ClientSession(timeout=timeout, auto_decompress=False)
        app["client_session"] = self._session

    async def stop(self, app: web.Application) -> None:
        session = app.get("client_session")
        if isinstance(session, ClientSession):
            await session.close()

    def _check_auth(self, request: web.Request) -> web.Response | None:
        token = _bearer_from_header(request.headers.get("Authorization"))
        if not self.proxy_key:
            return None
        if token != self.proxy_key:
            return web.json_response(
                {
                    "error": {
                        "message": "Invalid API key",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
                status=401,
            )
        return None

    async def models(self, request: web.Request) -> web.Response:
        auth_error = self._check_auth(request)
        if auth_error is not None:
            return auth_error
        created = int(time.time())
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": self.model_name,
                        "object": "model",
                        "created": created,
                        "owned_by": "codex-passthrough",
                        "permission": [],
                        "root": self.model_name,
                        "parent": None,
                    }
                ],
            }
        )

    async def proxy(self, request: web.Request) -> web.StreamResponse:
        auth_error = self._check_auth(request)
        if auth_error is not None:
            return auth_error

        assert self._session is not None
        upstream_token = _load_upstream_token()
        url = _upstream_url(request)
        if request.query_string:
            url = f"{url}?{request.query_string}"

        headers = _copy_headers(request.headers.items(), strip=REQUEST_STRIP_HEADERS)
        headers["Authorization"] = f"Bearer {upstream_token}"
        headers.setdefault("Accept", "text/event-stream")

        body = await request.read()
        logging.info("%s %s -> %s (%d bytes)", request.method, request.path_qs, url, len(body))

        try:
            async with self._session.request(
                request.method,
                url,
                headers=headers,
                data=body,
            ) as upstream:
                response_headers = _copy_headers(upstream.headers.items(), strip=RESPONSE_STRIP_HEADERS)
                response = web.StreamResponse(status=upstream.status, headers=response_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                logging.info("%s %s <- %d", request.method, request.path_qs, upstream.status)
                return response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("Proxy request failed: %s", exc)
            return web.json_response(
                {
                    "error": {
                        "message": f"Codex passthrough failed: {exc}",
                        "type": "server_error",
                    }
                },
                status=502,
            )


def create_app() -> web.Application:
    server = CodexPassthrough()
    app = web.Application(client_max_size=int(_env("CODEX_PROXY_MAX_BODY", str(128 * 1024 * 1024))))
    app.on_startup.append(server.start)
    app.on_cleanup.append(server.stop)
    app.router.add_get("/v1/models", server.models)
    app.router.add_route("*", "/v1/{tail:.*}", server.proxy)
    return app


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
