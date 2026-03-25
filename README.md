# ibkr-terminal — Interactive Brokers MCP Server

MCP server for Interactive Brokers. Real-time portfolio analytics, margin simulation, risk management, and market data — exposed as 34 tools over streamable HTTP with multi-account support.

<img src="assets/demo-output.png" width="100%" />

Technicals, performance comparison, and multi-tool orchestration — all from a single natural language prompt on claude.ai.

<img src="assets/demo-toolchain.png" width="100%" />

## Architecture

Connects to IB Gateway via `ib_insync` (asyncio-native TWS API wrapper), exposes tools through the Model Context Protocol. Dual transport: stdio for local CLI usage, streamable HTTP for remote access. Designed for headless deployment on a persistent server with multiple gateway instances.

**Transport**: Streamable HTTP (MCP) + stdio fallback + REST dashboard endpoints
**Accounts**: Multi-gateway — each IB Gateway instance runs on its own port with isolated client IDs
**Connection**: Lazy per-session IB connection with automatic reconnection and health monitoring
**Persistence**: Windows Task Scheduler for service lifecycle (survives SSH session termination)

## Tools

34 tools across 8 modules:

| Module | Count | Capabilities |
|--------|------:|-------------|
| **Account** | 6 | NAV, margin analysis (summary/efficiency/headroom), buying power, daily P&L, morning briefings, cross-account consolidated view |
| **Portfolio** | 3 | Positions with live P&L, portfolio snapshots, per-position P&L breakdown |
| **Market Data** | 6 | Real-time quotes (single/multi/portfolio), historical OHLCV bars, contract details, dividends, contract search, technicals (SMA/RSI/MACD/Bollinger) |
| **Live Data** | 4 | FX rates, intraday tick snapshots, options chains with Greeks, cross-symbol performance comparison |
| **Risk** | 4 | What-if margin simulation (buy/sell without placing orders), stress testing (custom scenarios), correlation matrix, Value-at-Risk estimation |
| **Intelligence** | 5 | Currency exposure breakdown, rebalance planning, sector decomposition, position deep-dive, portfolio beta vs benchmark |
| **Monitoring** | 4 | Daily movers, drawdown tracking from peak, risk dashboard with status flags, connection health diagnostics |
| **Orders** | 2 | Trade history (fills/journal/gains/completed), open order status |

## Dashboard API

REST endpoints served alongside MCP on the same process — no separate service. Built with `@mcp.custom_route()` for zero-overhead integration.

- `/api/portfolio` — Positions, P&L, account summary
- `/api/risk` — Margin utilization, stress scenarios
- `/api/query` — Natural language portfolio queries via LLM passthrough

## Stack

- `mcp` (FastMCP) — Model Context Protocol SDK
- `ib_insync` — IB TWS API, asyncio-native
- `uvicorn` — ASGI server for streamable HTTP transport
- `httpx` — Async HTTP client
- `pydantic` — Input validation and tool schemas
- `yfinance` — Supplemental market data for dashboard endpoints
