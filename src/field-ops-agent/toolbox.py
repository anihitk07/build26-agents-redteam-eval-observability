"""Toolbox MCP integration — Concept #1.

═══════════════════════════════════════════════════════════════════════════════
DEMO POINT — "Toolbox"

Talk track (from Demo-script.md, step 2):
    "Rather than building one-off tool integrations, I use Toolbox — a single
     place to organize, govern, and serve all my agent's tools. I add Work IQ
     here, and it's instantly available to any agent I build."

What to show:
  - This file is tiny on purpose. The agent just calls ``build_toolbox_tool()``
    and gets back a Toolbox-backed ``MCPStreamableHTTPTool`` it can drop into
    its tool list — same shape as a local function tool.
  - Tools live in Foundry's Toolbox (Work IQ, Document Understanding, WebSearch,
    etc.). They are *not* hardcoded in the agent.
  - Auth is bearer-token managed for us — the agent just declares the endpoint.
═══════════════════════════════════════════════════════════════════════════════

Environment variables:
  TOOLBOX_ENDPOINT
      Full Toolbox MCP endpoint URL. When unset, this module returns ``None``
      and the agent runs with local tools only (handy for quick demos).
  TOOLBOX_FEATURES
      Feature-flag header forwarded to the Toolbox proxy.
      Defaults to ``Toolboxes=V1Preview``.
"""

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
    """httpx auth that injects a fresh AAD bearer token on every request."""

    def __init__(self, token_provider):
        self._get_token = token_provider

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request


def _register_async_close(client: httpx.AsyncClient) -> None:
    """Best-effort cleanup so we don't leak the httpx connection pool on exit.

    The agent host owns the running event loop; here we just guarantee that
    when the process tears down (lifespan shutdown or atexit), the client is
    closed and "Unclosed client" warnings don't pollute the demo console.
    """
    def _close() -> None:
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                # Schedule on the running loop; ignore the future.
                asyncio.run_coroutine_threadsafe(client.aclose(), loop)
            else:
                asyncio.run(client.aclose())
        except Exception:
            logger.debug("Toolbox http_client close failed (non-fatal)", exc_info=True)

    atexit.register(_close)


def build_toolbox_tool(credential: DefaultAzureCredential) -> MCPStreamableHTTPTool | None:
    """Return a Toolbox-backed MCP tool, or ``None`` if Toolbox isn't configured.

    The returned tool can be dropped straight into the agent's ``tools=[...]``
    list alongside any local ``FunctionTool`` — that's the point of Toolbox.
    """
    endpoint = os.getenv("TOOLBOX_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("TOOLBOX_ENDPOINT not set — Toolbox tools disabled.")
        return None

    features = os.getenv("TOOLBOX_FEATURES", "Toolboxes=V1Preview")
    token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")

    http_client = httpx.AsyncClient(
        auth=_ToolboxAuth(token_provider),
        headers={"Foundry-Features": features} if features else {},
        timeout=120.0,
    )
    _register_async_close(http_client)

    logger.info("Connecting to Toolbox: %s", endpoint)
    return MCPStreamableHTTPTool(
        name="toolbox",
        url=endpoint,
        http_client=http_client,
        load_prompts=False,
    )
