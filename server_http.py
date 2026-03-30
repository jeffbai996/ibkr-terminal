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
# Live context dict — shared with MCP tool context. Background loop
# updates this in-place so tools always see current gateway state.
_live_ctx: dict = {}


def _sync_live_ctx() -> None:
    """Push current global state into the live context dict.

    The dict is yielded by http_lifespan and read by tools via
    ctx.request_context.lifespan_context. Updating in-place ensures
    tools always see the latest gateway connections, even when the
    secondary connects after the session started.
    """
    _live_ctx.update({
        "ib": _ib,
        "ib2": _ib2,
        "primary_account": _primary_account,
        "secondary_account": _secondary_account,
        "account_map": _account_map,
        "health": _health,
        "health_map": _health_map,
    })


async def _try_connect(host: str, port: int, client_id: int,
                       timeout: int, readonly: bool,
                       account: str = "") -> IB:
    """Attempt a single IB Gateway connection. Cleans up on failure.

    Returns connected IB instance or raises on failure.
    Always calls ib.disconnect() if connectAsync fails, so the gateway
    frees the clientId slot even on partial TCP connections.
    """
    ib = IB()
    try:
        kwargs: dict = {
            "host": host, "port": port, "clientId": client_id,
            "timeout": timeout, "readonly": readonly,
        }
        if account:
            kwargs["account"] = account
        await ib.connectAsync(**kwargs)
        return ib
    except Exception:
        try:
            ib.disconnect()
        except Exception:
            pass
        raise


async def _connect_ib() -> tuple[IB, str]:
    """Connect to primary IB Gateway.

    Tries the configured clientId first. If Error 326 (clientId in use),
    falls back to a random ID to bypass stale gateway sessions from a
    prior crash. Logs which ID was actually used.
    """
    import random
    global _ib, _health

    # Disconnect old instance FIRST to free the client ID slot on the gateway
    if _ib is not None:
        try:
            _ib.disconnect()
        except Exception:
            pass
        _ib = None

    # Try configured ID first, then random fallback
    ids_to_try = [IB_CLIENT_ID, random.randint(100, 999)]

    for i, client_id in enumerate(ids_to_try):
        label = "" if i == 0 else " (fallback)"
        logger.info(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT} "
                    f"(clientId={client_id}, readonly={IB_READONLY}){label}...")
        try:
            ib = await _try_connect(IB_HOST, IB_PORT, client_id,
                                    IB_TIMEOUT, IB_READONLY)

            # Attach health monitoring
            _health = ConnectionHealth()
            _health.attach(ib)

            accounts = ib.managedAccounts()
            primary = PRIMARY_ACCOUNT
            if not primary and accounts:
                primary = accounts[0]

            masked = [f"...{a[-4:]}" for a in accounts]
            logger.info(f"Connected{label}. {len(accounts)} account(s): {masked}. "
                        f"Primary: ...{primary[-4:] if primary else 'auto'}")
            return ib, primary
        except Exception as e:
            if i < len(ids_to_try) - 1:
                logger.warning(f"clientId {client_id} failed: {e}. "
                               "Trying fallback ID...")
                continue
            raise


async def _connect_ib2() -> tuple[IB | None, str]:
    """Connect to secondary IB Gateway. Returns (None, "") if not configured or fails.

    Same fallback strategy as _connect_ib() — tries configured ID first,
    then random on Error 326.
    """
    import random
    global _ib2, _health2

    if not IB_PORT_2:
        return None, ""

    # Disconnect old instance FIRST to free the client ID slot
    if _ib2 is not None:
        try:
            _ib2.disconnect()
        except Exception:
            pass
        _ib2 = None

    target_account = SECONDARY_ACCOUNT or ""
    ids_to_try = [IB_CLIENT_ID_2, random.randint(100, 999)]

    try:
        ib2 = None
        for i, client_id_2 in enumerate(ids_to_try):
            label = "" if i == 0 else " (fallback)"
            logger.info(f"Connecting to secondary IB Gateway at {IB_HOST}:{IB_PORT_2} "
                         f"(clientId={client_id_2}, account={target_account or 'auto'}){label}")
            try:
                ib2 = await asyncio.wait_for(
                    _try_connect(IB_HOST, IB_PORT_2, client_id_2,
                                 IB_TIMEOUT, IB_READONLY, account=target_account),
                    timeout=15,
                )
                break  # Connected
            except Exception as e:
                if i < len(ids_to_try) - 1:
                    logger.warning(f"Secondary clientId {client_id_2} failed: {e}. "
                                   "Trying fallback ID...")
                    continue
                raise
        if ib2 is None:
            raise RuntimeError("All client IDs failed")

        # Attach health monitoring
        _health2 = ConnectionHealth()
        _health2.attach(ib2)

        accounts_2 = ib2.managedAccounts()
        secondary = SECONDARY_ACCOUNT or (accounts_2[0] if accounts_2 else "")
        masked_2 = [f"...{a[-4:]}" for a in accounts_2]
        logger.info(f"Secondary connected. {len(accounts_2)} account(s): {masked_2}. "
                    f"Secondary: ...{secondary[-4:] if secondary else 'auto'}")

        # Diagnostic: confirm portfolio data populated after connectAsync(account=)
        n_positions = len(ib2.positions())
        n_portfolio = len(list(ib2.portfolio(secondary))) if secondary else 0
        logger.info(f"Secondary diagnostics: positions()={n_positions}, "
                    f"portfolio('{secondary}')={n_portfolio}")

        return ib2, secondary
    except Exception as e:
        # Clean up partial connection (TCP may be open even if order sync timed out)
        if ib2 is not None:
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

        try:
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
                        _sync_live_ctx()
                        logger.info("Background reconnect: primary gateway restored")
                    except Exception as e:
                        logger.warning(f"Background reconnect (primary) failed: {e}")

                # Secondary
                if IB_PORT_2 and not (
                    _ib2 is not None and _ib2.isConnected() and _health2.connected
                ):
                    try:
                        _ib2, _secondary_account = await _connect_ib2()
                        if _ib2:
                            for acc in _ib2.managedAccounts():
                                _account_map[acc] = _ib2
                                _health_map[acc] = _health2
                            _sync_live_ctx()
                            logger.info("Background reconnect: secondary gateway restored")
                    except Exception as e:
                        logger.warning(f"Background reconnect (secondary) failed: {e}")
        except Exception as e:
            # Catch-all so the loop never dies silently
            logger.error(f"Background reconnect loop error: {e}", exc_info=True)


@asynccontextmanager
async def http_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """
    Per-session lifespan — yields current IB state without blocking.

    NEVER attempts to connect. The background reconnect loop is solely
    responsible for establishing/restoring gateway connections. This
    prevents lock contention cascades when the gateway is down.

    Tools handle ib=None via GatewayOfflineError + @cached_tool.
    """
    global _reconnect_task

    # Initialize persistence DB (idempotent, first call only)
    from core.persistence import init_db
    init_db()

    # Start background reconnect loop if not already running.
    # This is the ONLY place connections are attempted.
    if _reconnect_task is None or _reconnect_task.done():
        _reconnect_task = asyncio.create_task(
            _background_reconnect_loop(),
            name="ibkr_reconnect",
        )

    # No lock, no connect attempt. Just sync and yield current state.
    _sync_live_ctx()
    yield _live_ctx


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
import tools.briefing     # noqa: F401, E402

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
    logger.info(f"Config: IB_PORT={IB_PORT}, IB_CLIENT_ID={IB_CLIENT_ID}, "
                f"IB_PORT_2={IB_PORT_2}, IB_CLIENT_ID_2={IB_CLIENT_ID_2}, "
                f"SECONDARY_ACCOUNT={SECONDARY_ACCOUNT or '(auto)'}")

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

    def _shutdown_ib():
        """Disconnect IB connections on server shutdown to free client IDs."""
        global _ib, _ib2
        for label, conn in [("primary", _ib), ("secondary", _ib2)]:
            if conn is not None:
                try:
                    conn.disconnect()
                    logger.info(f"Disconnected {label} IB connection.")
                except Exception as e:
                    logger.warning(f"Error disconnecting {label}: {e}")
        _ib = None
        _ib2 = None

    import atexit
    atexit.register(_shutdown_ib)

    uvicorn.run(starlette_app, host=MCP_HTTP_HOST, port=MCP_HTTP_PORT)
