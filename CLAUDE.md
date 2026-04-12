# ibkr-terminal — Claude Code Project Guide

## What This Is

MCP server for Interactive Brokers. Exposes 32 read-only portfolio analytics tools over streamable HTTP (and stdio for local use). Deployed on fragserv (Windows/WSL) behind Tailscale Funnel. Includes a REST dashboard API that powers a Vite+React frontend.

All tool logic lives in the sibling `ibkr-terminal-core` package (private, installed as editable). This repo holds server entry points, configuration, dashboard REST endpoints, and deployment config.

## Project Structure

```
server_http.py       # HTTP transport entry point (production — used by claude.ai)
server.py            # stdio transport entry point (local Claude Code)
app.py               # Shared FastMCP instance (tools import mcp from here)
config.py            # Environment variables → Python constants
dashboard.py         # REST API endpoints via @mcp.custom_route()
risk_factors.json    # Geopolitical risk factor definitions
thesis_config.json   # Investment thesis pillar definitions
env.example          # Template for .env
requirements.txt     # Python dependencies
DEPLOY.md            # Deployment and operations guide
tests/               # Server-level tests (what-if, analytics)
logs/                # Server log output (gitignored)
```

## Running

```bash
# Local (stdio — for Claude Code MCP integration)
source venv/bin/activate
python server.py

# Production (HTTP — for remote clients)
source venv/bin/activate
nohup python3 server_http.py >> logs/server.log 2>&1 & disown
```

## Running Tests

```bash
source venv/bin/activate
pytest tests/ -x -v
```

Tests use mocks from `ibkr-terminal-core/tests/conftest.py`. No IB Gateway needed.

## Architecture

### Two-Transport Model

- **`server.py`** (stdio): Local Claude Code. Uses `ib_lifespan` from `core.connection` — connects once at startup, disconnects on shutdown.
- **`server_http.py`** (HTTP): Remote clients (claude.ai). Uses `http_lifespan` — fires per-session but never blocks. Background reconnect loop (30s interval) is the sole connection path.

Both share the same `mcp` object from `app.py` and register the same 9 tool modules.

### The `app.mcp` Swap

`server_http.py` line ~390 replaces `app.mcp` with an HTTP-configured FastMCP instance BEFORE importing tool modules. This ensures all `@mcp.tool()` decorators register on the HTTP server, not the default stdio one. Do not reorder these imports.

### Connection Management

```
server_http.py globals:
  _ib, _ib2              → Primary/secondary IB connections
  _account_map           → {account_id: IB} routing table
  _health, _health_map   → ConnectionHealth per gateway/account
  _live_ctx              → Dict synced in-place, yielded to tools via lifespan
  _ib_lock               → Guards connection attempts
```

- **Background reconnect loop** (`_background_reconnect_loop`): Runs every 30s, reconnects dropped gateways, evicts stale cache entries. This is the ONLY code that creates IB connections.
- **Fallback client IDs**: On Error 326 (clientId in use), retries with `clientId + 100` (fixed offset, not random — avoids gateway tab explosion).
- **`_sync_live_ctx()`**: Updates the `_live_ctx` dict in-place after any connection change. Tools always see current state.

### Dashboard (dashboard.py)

REST endpoints served on the same port via `@mcp.custom_route()`. Two categories:

1. **JSON endpoints** (`/api/summary`, `/api/positions`, `/api/prices`, `/api/health`): Return structured data for the React frontend.
2. **Tool proxy endpoints** (`/api/margin`, `/api/stress`, `/api/trades`, etc.): Call core tool functions directly, return `{"result": "<markdown>"}`.

The `_FakeContext` shim wraps `_live_ctx` so tool functions think they're in an MCP lifespan.

### The `__main__` Bug (CRITICAL)

`dashboard.py` uses `sys.modules["__main__"]` (via `_sh()` helper) to access server globals (`_ib`, `_account_map`, etc.). NEVER use `import server_http as sh` — that re-imports the module, creating a fresh copy with all globals reset to None. This was a real bug that caused empty health endpoints.

```python
# CORRECT
def _sh():
    import sys
    return sys.modules["__main__"]

# WRONG — creates fresh module with _ib=None
import server_http as sh
```

## Configuration

All config via `.env` (see `env.example`). Key variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `IB_PORT` | 4001 | Primary IB Gateway port |
| `IB_CLIENT_ID` | 10 | Primary client ID |
| `IB_PORT_2` | 0 | Secondary gateway (0 = disabled) |
| `IB_CLIENT_ID_2` | 20 | Secondary client ID |
| `MCP_HTTP_PORT` | 8000 | HTTP listen port |
| `MCP_HTTP_HOST` | 0.0.0.0 | Bind address |
| `ANTHROPIC_API_KEY` | — | Claude API for `/api/query` passthrough |
| `MCP_URL` | — | MCP endpoint URL for Claude callback |
| `DASHBOARD_DIST` | ../ibkr-dashboard/frontend/dist | Vite build output |
| `YAHOO_CACHE_TTL` | 30 | Yahoo Finance price cache TTL (seconds) |
| `IBKR_DB_PATH` | ~/.ibkr-terminal/history.db | SQLite NLV history |

## HTTP Server Settings

```python
mcp.settings.json_response = True      # claude.ai sends Accept: application/json only
mcp.settings.stateless_http = True     # No session IDs — each request independent
TransportSecuritySettings(enable_dns_rebinding_protection=False)  # Required for Tailscale Funnel
```

## Deployment

See `DEPLOY.md` for full ops guide. Quick reference:

- **Server**: fragserv (Windows + WSL Ubuntu, user `jbai`)
- **Repos**: `/home/jbai/repos/ibkr-terminal` and `/home/jbai/repos/ibkr-terminal-core`
- **Venv**: `/home/jbai/repos/ibkr-terminal/venv`
- **Daily restart**: Cron at 5am (`pkill -f server_http.py; sleep 2; nohup python3 server_http.py ...`)
- **Public URL**: `https://fragserv.tailab4af9.ts.net/mcp` via Tailscale Funnel

## Gotchas

- **IB Gateway daily restart**: Gateway forces daily restart (~midnight US). Server reconnects via background loop within 30s. Known issue: sometimes gateway comes back "green" but API socket doesn't bind — requires gateway process restart.
- **`nest_asyncio.apply()`**: Must happen before ANY imports that touch asyncio. First line of both server.py and server_http.py.
- **Port cleanup**: `_free_port()` kills stale processes on the configured port at startup (SIGTERM then SIGKILL after 2s).
- **Error 10185**: Harmless PnL cancel on non-existent subscription. Wrapped in try/except in core.
- **Yahoo Finance in dashboard**: Used because IB market data subscriptions aren't always available. Cached with configurable TTL.
