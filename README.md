# ibkr-terminal

MCP server for Interactive Brokers. Streamable HTTP transport with multi-account support, real-time portfolio analytics, and a REST dashboard API.

## Architecture

Connects to IB Gateway via `ib_insync` (asyncio-native TWS API wrapper), exposes tools through the Model Context Protocol over streamable HTTP. Designed for headless deployment on a persistent server with multiple gateway instances.

**Transport**: Streamable HTTP (MCP) + REST endpoints for dashboard consumption
**Accounts**: Multi-gateway — each IB Gateway instance runs on its own port with isolated client IDs
**Persistence**: Windows Task Scheduler for service lifecycle (survives SSH session termination)

## Tools

| Category | Count | Capabilities |
|----------|------:|-------------|
| **Account** | 4 | NAV, margin requirements, buying power, daily/unrealized P&L |
| **Portfolio** | 4 | Positions with Greeks, currency exposure, concentration analysis, snapshots |
| **Market Data** | 3 | Real-time quotes, historical OHLCV bars, contract specifications |
| **Risk** | 4 | What-if margin scenarios, stress testing, concentration risk, portfolio heat |
| **Intelligence** | 2 | Cross-account aggregation, sector/asset-class decomposition |

## Dashboard API

REST endpoints served alongside MCP on the same process — no separate service. Built with `@mcp.custom_route()` for zero-overhead integration.

- `/api/portfolio` — Positions, P&L, account summary
- `/api/risk` — Margin utilization, stress scenarios
- `/api/query` — Natural language portfolio queries via LLM passthrough

## Stack

- `mcp` (FastMCP) — Model Context Protocol SDK
- `ib_insync` — IB TWS API, asyncio-native
- `httpx` — HTTP transport layer
- `pydantic` — Input validation and tool schemas
