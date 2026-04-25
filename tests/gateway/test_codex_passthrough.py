from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectionResetError, web

from gateway.platforms.codex_passthrough import CodexPassthrough


class _FakeUpstreamResponse:
    def __init__(self, *, status: int = 200, headers: dict[str, str] | None = None, chunks=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = self
        self._chunks = chunks if chunks is not None else [b'{"ok":true}']

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def iter_chunked(self, size: int):
        del size
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_proxy_returns_started_response_when_write_eof_sees_client_disconnect():
    server = CodexPassthrough(proxy_key="sk-test")
    server._session = MagicMock()
    server._session.request.return_value = _FakeUpstreamResponse()

    request = MagicMock()
    request.method = "POST"
    request.path_qs = "/v1/responses"
    request.query_string = ""
    request.match_info = {"tail": "responses"}
    request.headers.items.return_value = [("Authorization", "Bearer sk-test")]
    request.headers.get.side_effect = lambda key, default=None: {"Authorization": "Bearer sk-test"}.get(key, default)
    request.read = AsyncMock(return_value=b'{"input":"pong"}')

    response = MagicMock()
    response.prepare = AsyncMock()
    response.write = AsyncMock()
    response.write_eof = AsyncMock(side_effect=ClientConnectionResetError("closing transport"))

    with (
        patch("gateway.platforms.codex_passthrough._load_upstream_token", return_value="upstream-token"),
        patch("gateway.platforms.codex_passthrough.web.StreamResponse", return_value=response),
    ):
        result = await server.proxy(request)

    assert result is response
    response.prepare.assert_awaited_once()
    response.write.assert_awaited()
    response.write_eof.assert_awaited_once()


@pytest.mark.asyncio
async def test_proxy_returns_499_when_client_disconnects_before_response_prepare():
    server = CodexPassthrough(proxy_key="sk-test")
    server._session = MagicMock()
    server._session.request.side_effect = ClientConnectionResetError("client went away")

    request = MagicMock()
    request.method = "POST"
    request.path_qs = "/v1/responses"
    request.query_string = ""
    request.match_info = {"tail": "responses"}
    request.headers.items.return_value = [("Authorization", "Bearer sk-test")]
    request.headers.get.side_effect = lambda key, default=None: {"Authorization": "Bearer sk-test"}.get(key, default)
    request.read = AsyncMock(return_value=b'{"input":"pong"}')

    with patch("gateway.platforms.codex_passthrough._load_upstream_token", return_value="upstream-token"):
        result = await server.proxy(request)

    assert isinstance(result, web.Response)
    assert result.status == 499
