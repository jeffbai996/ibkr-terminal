# Deployment Guide

Internal deployment docs for the ibkr-terminal MCP server. This server depends on `ibkr-terminal-core` (private, editable install) and IB Gateway instances running locally.

## Server Overview

Single `server_http.py` process serving MCP over streamable HTTP. Connects to one or two IB Gateway instances via `ib_insync`. Exposed to the internet via Tailscale Funnel (HTTPS, non-guessable URL). All tools are read-only.

## Prerequisites

- Python 3.11+ with `venv` module
- IB Gateway running (port 4001 primary, optionally 4002 secondary)
- Tailscale installed with Funnel enabled
- `ibkr-terminal-core` repo cloned as sibling directory

## Initial Setup

```bash
# Clone both repos side by side
git clone <ibkr-terminal-url> ibkr-terminal
git clone <ibkr-terminal-core-url> ibkr-terminal-core

# Create venv and install
cd ibkr-terminal
python3 -m venv venv
source venv/bin/activate
pip install -e ../ibkr-terminal-core -r requirements.txt

# Create .env from example
cp env.example .env
# Edit .env with your values (see Configuration below)

# Create logs directory
mkdir -p logs
```

## Configuration

Copy `env.example` to `.env` and configure. Key settings:

| Variable | Required | Description |
|----------|----------|-------------|
| `IB_PORT` | Yes | IB Gateway port (4001 default) |
| `IB_CLIENT_ID` | Yes | Unique client ID (10 default) |
| `MCP_HTTP_PORT` | Yes | HTTP listen port — must match Tailscale Funnel target |
| `IB_PORT_2` | No | Secondary gateway port for dual-account mode |
| `SECONDARY_ACCOUNT` | No | Account ID on secondary gateway |

The server reads `.env` via `python-dotenv` on startup. Changes require a restart.

## Starting the Server

```bash
cd /path/to/ibkr-terminal
source venv/bin/activate
nohup python3 server_http.py >> logs/server.log 2>&1 & disown
```

The server:
1. Binds to `MCP_HTTP_PORT` immediately
2. Background reconnect loop connects to IB Gateway(s) within 30 seconds
3. Tools return cached/error responses while gateways are offline (`@cached_tool`)
4. Automatically reconnects when gateways come back (every 30s check)

## Stopping the Server

```bash
# Find the PID
ps aux | grep server_http | grep -v grep

# Graceful shutdown
kill <PID>
```

The server cleans up IB connections on shutdown via `atexit` handler.

## Updating

```bash
# Pull both repos
cd /path/to/ibkr-terminal-core && git pull
cd /path/to/ibkr-terminal && git pull

# Reinstall core if dependencies changed
source venv/bin/activate
pip install -e ../ibkr-terminal-core

# Restart server
kill <old PID>
nohup python3 server_http.py >> logs/server.log 2>&1 & disown
```

**For test-only changes**: `git pull` is sufficient — no restart needed.

## Tailscale Funnel

The server must be exposed via Tailscale Funnel for remote MCP clients (claude.ai, Claude Code via mcp-remote).

```bash
# One-time setup (run from the host, not WSL)
tailscale funnel --set-path / proxy http://127.0.0.1:<MCP_HTTP_PORT>
```

Verify with `tailscale funnel status`. The Funnel URL is your MCP endpoint + `/mcp`.

## Health Check

```bash
# Local
curl http://localhost:<MCP_HTTP_PORT>/api/health

# Remote (through Funnel)
curl https://<your-machine>.<tailnet>.ts.net/api/health
```

Returns `{"status":"ok","accounts":["...", ...]}`. Empty accounts list means gateways haven't connected yet (wait 30s for reconnect loop).

## Logs

- **Server log**: `logs/server.log` (if started with `>> logs/server.log`)
- **Connection events**: Look for `ibkr_mcp.http` logger entries
- **IB errors**: Look for `ib_insync.wrapper` Error lines (e.g., 326 = clientId in use, 10185 = PnL cancel on non-existent subscription)

## Troubleshooting

### Server won't start — ModuleNotFoundError
Venv not activated. Run `source venv/bin/activate` first.

### Accounts empty after 60+ seconds
IB Gateway not running, or clientId conflict. Check:
```bash
# Is gateway reachable?
nc -zv 127.0.0.1 4001

# Is another process holding the clientId?
grep "Error 326" logs/server.log
```

### Port already in use
The server auto-kills stale processes on the configured port at startup. If that fails:
```bash
lsof -ti :<PORT> | xargs kill -9
```

### Tailscale Funnel not routing
Verify Funnel target port matches `MCP_HTTP_PORT` in `.env`:
```bash
tailscale funnel status
```

### PnL cancel errors (10185)
Harmless — happens when a tool's `finally` block cancels a subscription that was never created (e.g., gateway disconnected mid-request). Wrapped in try/except, won't crash the server.

## Architecture Notes

- **Two-repo split**: `ibkr-terminal` has server entry points and config. `ibkr-terminal-core` has all tools and core logic, installed as an editable package.
- **Dual transport**: `server.py` (stdio, for local CLI) and `server_http.py` (HTTP, for remote). Same tools, different lifespan.
- **Stateless HTTP**: No MCP session IDs. Each request is independent. Safe because all state lives in IB Gateway.
- **Background reconnect**: `_background_reconnect_loop()` runs every 30s, attempts to restore dropped gateway connections. This is the only code path that creates IB connections.
