"""Shared Codex passthrough aiohttp app for standalone and mounted use."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable

from aiohttp import ClientConnectionResetError, ClientSession, ClientTimeout, web


DEFAULT_UPSTREAM_BASE = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
DEFAULT_PROXY_KEY = "replace-with-your-own-key"
DEFAULT_PROXY_MODEL = "gpt-5.4"
DEFAULT_MAX_BODY_BYTES = 128 * 1024 * 1024
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

logger = logging.getLogger(__name__)


def _is_client_disconnect(exc: BaseException) -> bool:
    return isinstance(exc, (BrokenPipeError, ClientConnectionResetError, ConnectionResetError))


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else default


def resolve_codex_proxy_max_body() -> int:
    raw = _env("CODEX_PROXY_MAX_BODY", str(DEFAULT_MAX_BODY_BYTES))
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BODY_BYTES


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


class CodexPassthrough:
    def __init__(
        self,
        *,
        proxy_key: str | None = None,
        model_name: str | None = None,
        upstream_base: str | None = None,
    ) -> None:
        self.proxy_key = proxy_key if proxy_key is not None else _env("CODEX_PROXY_KEY", DEFAULT_PROXY_KEY)
        self.model_name = model_name or _env("CODEX_PROXY_MODEL", DEFAULT_PROXY_MODEL)
        self.upstream_base = (upstream_base or _env("CODEX_UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE)).rstrip("/")
        self._session: ClientSession | None = None

    async def start(self, app: web.Application) -> None:
        timeout = ClientTimeout(total=None, sock_connect=30, sock_read=None)
        self._session = ClientSession(timeout=timeout, auto_decompress=False)
        app["client_session"] = self._session

    async def stop(self, app: web.Application) -> None:
        session = app.get("client_session")
        if session is not None and hasattr(session, "close"):
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

    def _upstream_url(self, request: web.Request) -> str:
        tail = request.match_info.get("tail")
        suffix = f"/{tail}" if tail else ""
        return f"{self.upstream_base}{suffix}"

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
        url = self._upstream_url(request)
        if request.query_string:
            url = f"{url}?{request.query_string}"

        headers = _copy_headers(request.headers.items(), strip=REQUEST_STRIP_HEADERS)
        headers["Authorization"] = f"Bearer {upstream_token}"
        headers.setdefault("Accept", "text/event-stream")

        body = await request.read()
        logger.info("%s %s -> %s (%d bytes)", request.method, request.path_qs, url, len(body))

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
                try:
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await response.write(chunk)
                    await response.write_eof()
                except Exception as exc:
                    if _is_client_disconnect(exc):
                        logger.info(
                            "Client disconnected while streaming %s %s",
                            request.method,
                            request.path_qs,
                        )
                        return response
                    raise
                logger.info("%s %s <- %d", request.method, request.path_qs, upstream.status)
                return response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_client_disconnect(exc):
                logger.info("Client disconnected before response was ready for %s %s", request.method, request.path_qs)
                return web.Response(status=499)
            logger.exception("Proxy request failed: %s", exc)
            return web.json_response(
                {
                    "error": {
                        "message": f"Codex passthrough failed: {exc}",
                        "type": "server_error",
                    }
                },
                status=502,
            )


def create_codex_passthrough_app(
    *,
    proxy_key: str | None = None,
    model_name: str | None = None,
    upstream_base: str | None = None,
) -> web.Application:
    server = CodexPassthrough(
        proxy_key=proxy_key,
        model_name=model_name,
        upstream_base=upstream_base,
    )
    app = web.Application(client_max_size=resolve_codex_proxy_max_body())
    app.on_startup.append(server.start)
    app.on_cleanup.append(server.stop)
    app.router.add_get("/v1/models", server.models)
    app.router.add_route("*", "/v1", server.proxy)
    app.router.add_route("*", "/v1/{tail:.*}", server.proxy)
    return app
