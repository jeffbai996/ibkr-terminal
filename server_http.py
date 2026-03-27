#!/usr/bin/env python3
"""
ibkr_mcp — HTTP transport entry point for remote MCP clients (claude.ai).

Runs the same MCP server as server.py but over streamable HTTP instead of
stdio. This lets claude.ai (and any other remote MCP client) connect over
the network.

KEY DIFFERENCE FROM server.py:
In streamable HTTP, the lifespan fires per-session (not once at startup
like stdio). So we connect to IB Gateway lazily on the first session and
cache the connection for all subsequent sessions.

Supports dual-gateway mode: if IB_PORT_2 is set, lazily connects to a
second IB Gateway instance alongside the primary.

Security: Designed to run behind Tailscale Funnel, which provides HTTPS
and a non-guessable URL. All tools are read-only (no order placement).
Do NOT expose this directly to the public internet without auth.

Usage:
    # Run the HTTP server
    python server_http.py

    # Then expose via Tailscale Funnel (separate terminal)
    tailscale funnel 8000

    # Add the Funnel URL to claude.ai:
    # Settings > Connectors > Add > https://<your-machine>.<tailnet>.ts.net
"""

# Patch asyncio before anything else — same reason as server.py
import nest_asyncio
nest_asyncio.apply()

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn

from dotenv import load_dotenv
load_dotenv()

from ib_insync import IB
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from config import (
    IB_HOST, IB_PORT, IB_CLIENT_ID, IB_TIMEOUT, IB_READONLY,
    PRIMARY_ACCOUNT, IB_PORT_2, IB_CLIENT_ID_2, SECONDARY_ACCOUNT,
)
from core.connection import ConnectionHealth

logger = logging.getLogger("ibkr_mcp.http")

# --- Shared IB connections (lazily created on first session) ---
_ib: IB | None = None
_ib2: IB | None = None
_primary_account: str = ""
_secondary_account: str = ""
_account_map: dict[str, IB] = {}
_health: ConnectionHealth = ConnectionHealth()
_health2: ConnectionHealth = ConnectionHealth()
_health_map: dict[str, ConnectionHealth] = {}
_ib_lock = asyncio.Lock()


async def _connect_ib() -> tuple[IB, str]:
    """Connect to primary IB Gateway.

    Keeps the old IB instance alive until the new one connects, so
    ib_insync's internal subscription cache (positions, account data)
    survives failed reconnection attempts.
    """
    global _ib, _health

    ib = IB()
    client_id = IB_CLIENT_ID + 5000 + (os.getpid() % 5000)

    logger.info(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} "
                f"(clientId={client_id}, readonly={IB_READONLY})...")

    await ib.connectAsync(
        host=IB_HOST,
        port=IB_PORT,
        clientId=client_id,
        timeout=IB_TIMEOUT,
        readonly=IB_READONLY,
    )

    # New connection succeeded — now safe to clean up old one
    if _ib is not None:
        try:
            _ib.disconnect()
        except Exception:
            pass

    # Attach health monitoring
    _health = ConnectionHealth()
    _health.attach(ib)

    accounts = ib.managedAccounts()
    primary = PRIMARY_ACCOUNT
    if not primary and accounts:
        primary = accounts[0]

    masked = [f"...{a[-4:]}" for a in accounts]
    logger.info(f"Connected. {len(accounts)} account(s): {masked}. "
                f"Primary: ...{primary[-4:] if primary else 'auto'}")
    return ib, primary


async def _connect_ib2() -> tuple[IB | None, str]:
    """Connect to secondary IB Gateway. Returns (None, "") if not configured or fails."""
    global _ib2, _health2

    if not IB_PORT_2:
        return None, ""

    ib2 = IB()
    client_id_2 = IB_CLIENT_ID_2 + 5000 + (os.getpid() % 5000)

    try:
        logger.info(f"Connecting to secondary IB Gateway at {IB_HOST}:{IB_PORT_2} "
                     f"(clientId={client_id_2})...")
        # Hard 15s cap — ib_insync's internal order sync (reqOpenOrders +
        # reqCompletedOrders) can hang 30-40s if the gateway accepts TCP
        # but isn't responding to requests (e.g. TWS session locked).
        # The IB_TIMEOUT param only covers the TCP handshake, not the sync.
        await asyncio.wait_for(
            ib2.connectAsync(
                host=IB_HOST,
                port=IB_PORT_2,
                clientId=client_id_2,
                timeout=IB_TIMEOUT,
                readonly=IB_READONLY,
            ),
            timeout=15,
        )

        # New connection succeeded — clean up old one
        if _ib2 is not None:
            try:
                _ib2.disconnect()
            except Exception:
                pass

        # Attach health monitoring
        _health2 = ConnectionHealth()
        _health2.attach(ib2)

        accounts_2 = ib2.managedAccounts()
        secondary = SECONDARY_ACCOUNT or (accounts_2[0] if accounts_2 else "")
        masked_2 = [f"...{a[-4:]}" for a in accounts_2]
        logger.info(f"Secondary connected. {len(accounts_2)} account(s): {masked_2}. "
                    f"Secondary: ...{secondary[-4:] if secondary else 'auto'}")
        return ib2, secondary
    except Exception as e:
        # Clean up partial connection (TCP may be open even if order sync timed out)
        try:
            ib2.disconnect()
        except Exception:
            pass
        msg = "timed out (15s)" if isinstance(e, asyncio.TimeoutError) else str(e)
        logger.warning(f"Failed to connect to secondary IB Gateway on port {IB_PORT_2}: {msg}. "
                       "Continuing with primary only.")
        return None, ""


_reconnect_task: asyncio.Task | None = None


async def _background_reconnect_loop() -> NoReturn:
    """Periodically try to reconnect to IB Gateway(s) when offline.

    Runs every 30s. When the gateway comes back (e.g., after wife logs
    out of TWS), this restores the connection automatically without
    needing a new MCP session.
    """
    global _ib, _ib2, _primary_account, _secondary_account, _account_map, _health_map

    # Try immediately on first run — secondary was deferred from lifespan
    _first_run = True

    while True:
        if _first_run:
            _first_run = False
            await asyncio.sleep(1)  # Brief yield to let lifespan finish
        else:
            await asyncio.sleep(30)

        # Primary: skip if already connected
        primary_ok = _ib is not None and _ib.isConnected() and _health.connected
        secondary_ok = (
            not IB_PORT_2
            or (_ib2 is not None and _ib2.isConnected() and _health2.connected)
        )

        if primary_ok and secondary_ok:
            continue

        async with _ib_lock:
            # Re-check after acquiring lock
            if not (_ib is not None and _ib.isConnected() and _health.connected):
                try:
                    _ib, _primary_account = await _connect_ib()
                    _account_map = {}
                    _health_map = {}
                    for acc in _ib.managedAccounts():
                        _account_map[acc] = _ib
                        _health_map[acc] = _health
                    logger.info("Background reconnect: primary gateway restored")
                except Exception as e:
                    logger.debug(f"Background reconnect (primary) failed: {e}")

            # Secondary
            if IB_PORT_2 and not (
                _ib2 is not None and _ib2.isConnected() and _health2.connected
            ):
                _ib2, _secondary_account = await _connect_ib2()
                if _ib2:
                    for acc in _ib2.managedAccounts():
                        _account_map[acc] = _ib2
                        _health_map[acc] = _health2
                    logger.info("Background reconnect: secondary gateway restored")


@asynccontextmanager
async def http_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """
    Per-session lifespan that lazily connects to IB Gateway(s).

    RESILIENT: Never raises on connection failure. If the gateway is
    down, yields with ib=None (or stale ib). Tools use the @cached_tool
    decorator to serve last-known data when the gateway is offline.

    A background task periodically retries connection so the gateway
    is restored automatically when it comes back.
    """
    global _ib, _ib2, _primary_account, _secondary_account, _account_map, _health_map
    global _reconnect_task

    # Initialize persistence DB (idempotent, first call only)
    from core.persistence import init_db
    init_db()

    async with _ib_lock:
        # Primary connection — check TCP + upstream health
        needs_reconnect = (
            _ib is None
            or not _ib.isConnected()
            or not _health.connected
        )
        if needs_reconnect:
            try:
                _ib, _primary_account = await _connect_ib()
                _account_map = {}
                _health_map = {}
                for acc in _ib.managedAccounts():
                    _account_map[acc] = _ib
                    _health_map[acc] = _health
            except Exception as e:
                logger.warning(
                    f"IB Gateway unavailable: {e}. "
                    "MCP server will continue with cached data. "
                    "Background reconnect will retry every 30s."
                )

        # Secondary connection — handled by background reconnect loop, NOT here.
        # _connect_ib2() can hang 15-40s if the gateway accepts TCP but doesn't
        # respond to order sync, which blocks lifespan and causes ClientDisconnect
        # cascades as Claude Code retries against the held _ib_lock.

        # Ensure background reconnect loop is running (tries secondary immediately)
        if _reconnect_task is None or _reconnect_task.done():
            _reconnect_task = asyncio.create_task(
                _background_reconnect_loop(),
                name="ibkr_reconnect",
            )

    yield {
        "ib": _ib,
        "ib2": _ib2,
        "primary_account": _primary_account,
        "secondary_account": _secondary_account,
        "account_map": _account_map,
        "health": _health,
        "health_map": _health_map,
    }


# --- Replace app.py's mcp BEFORE tool imports ---
# Tool modules do `from app import mcp` and decorate with @mcp.tool().
# By swapping app.mcp before those imports run, all tools register on
# our HTTP-configured instance with the lazy-connect lifespan.
import app  # noqa: E402
app.mcp = FastMCP("ibkr_mcp", lifespan=http_lifespan)
mcp = app.mcp

# --- Register tool modules (they import mcp from app.py) ---
import tools.account      # noqa: F401, E402
import tools.portfolio    # noqa: F401, E402
import tools.market_data  # noqa: F401, E402
import tools.orders       # noqa: F401, E402
import tools.live_data    # noqa: F401, E402
import tools.risk         # noqa: F401, E402
import tools.intelligence # noqa: F401, E402
import tools.monitoring   # noqa: F401, E402
import tools.export       # noqa: F401, E402

# --- Dashboard REST routes (registered via @mcp.custom_route) ---
import dashboard  # noqa: F401, E402

MCP_HTTP_HOST = os.environ.get("MCP_HTTP_HOST", "0.0.0.0")
MCP_HTTP_PORT = int(os.environ.get("MCP_HTTP_PORT", "8000"))

# Dashboard frontend dist directory (built by Vite)
DASHBOARD_DIST = os.environ.get(
    "DASHBOARD_DIST",
    os.path.join(os.path.dirname(__file__), "..", "ibkr-dashboard", "frontend", "dist"),
)


if __name__ == "__main__":
    import uvicorn
    from pathlib import Path
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info(f"Starting ibkr_mcp HTTP server on {MCP_HTTP_HOST}:{MCP_HTTP_PORT}")
    logger.info("Transport: streamable-http")
    logger.info("Endpoint: /mcp/")

    mcp.settings.host = MCP_HTTP_HOST
    mcp.settings.port = MCP_HTTP_PORT

    # JSON response mode — claude.ai's initial probe only sends
    # Accept: application/json (without text/event-stream), which the
    # MCP SDK rejects with 406 in default SSE mode. JSON mode relaxes
    # the Accept header check to only require application/json.
    mcp.settings.json_response = True

    # Disable DNS rebinding protection — requests come through Tailscale
    # Funnel with a non-localhost Host header, not 0.0.0.0
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

    # Get the Starlette app from FastMCP, then add static file serving
    starlette_app = mcp.streamable_http_app()

    dist_dir = Path(DASHBOARD_DIST)
    if dist_dir.exists():
        # Append static file mount AFTER MCP + API routes (lower priority)
        starlette_app.router.routes.append(
            Mount("/", app=StaticFiles(directory=str(dist_dir), html=True), name="dashboard")
        )
        logger.info(f"Dashboard: serving frontend from {dist_dir}")
    else:
        logger.info(f"Dashboard: no frontend found at {dist_dir} (API-only mode)")

    logger.info("To expose via Tailscale Funnel: tailscale funnel %d", MCP_HTTP_PORT)

    uvicorn.run(starlette_app, host=MCP_HTTP_HOST, port=MCP_HTTP_PORT)
