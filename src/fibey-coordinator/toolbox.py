"""Toolbox MCP integration for fibey-coordinator."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os

import httpx
from agent_framework import MCPStreamableHTTPTool
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

logger = logging.getLogger(__name__)


class _ToolboxAuth(httpx.Auth):
    """Inject a fresh AAD bearer token for each toolbox request."""

    def __init__(self, token_provider):
        self._get_token = token_provider

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request


def _register_async_close(client: httpx.AsyncClient) -> None:
    """Best-effort cleanup for the async client connection pool."""

    def _close() -> None:
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(client.aclose(), loop)
            else:
                asyncio.run(client.aclose())
        except Exception:
            logger.debug("Toolbox http_client close failed (non-fatal)", exc_info=True)

    atexit.register(_close)


def build_toolbox_tool(credential: DefaultAzureCredential) -> MCPStreamableHTTPTool | None:
    """Return a Toolbox-backed MCP tool, or None when not configured."""

    endpoint = os.getenv("TOOLBOX_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("TOOLBOX_ENDPOINT not set — toolbox tools disabled.")
        return None

    features = os.getenv("TOOLBOX_FEATURES", "Toolboxes=V1Preview")
    token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")

    http_client = httpx.AsyncClient(
        auth=_ToolboxAuth(token_provider),
        headers={"Foundry-Features": features} if features else {},
        timeout=120.0,
    )
    _register_async_close(http_client)

    logger.info("Connecting to toolbox endpoint: %s", endpoint)
    return MCPStreamableHTTPTool(
        name="toolbox",
        url=endpoint,
        http_client=http_client,
        load_prompts=False,
    )
