# ibkr-terminal — Interactive Brokers MCP Server

MCP server for Interactive Brokers. Real-time portfolio analytics, margin simulation, risk management, and market data — exposed as 32 tools over streamable HTTP with multi-account support.

<p align="center">
<img src="assets/demo-dashboard.png" width="90%" />
</p>

<p align="center"><em>Full account dashboard rendered as a claude.ai artifact — margin health, VaR, drawdown tracking, P&L heatmap, position weights, and correlation matrix from a single prompt.</em></p>

## Architecture

Two-repo structure: `ibkr-terminal` (server entry points, config, tests) and `ibkr-terminal-core` (all tools and core logic, installed as editable package). Connects to IB Gateway via `ib_insync`, exposes tools through the Model Context Protocol. Dual transport: stdio for local CLI, streamable HTTP for remote access. Designed for headless deployment on a persistent server with multiple gateway instances.

**Transport**: Streamable HTTP (MCP) + stdio fallback + REST dashboard endpoints
**Accounts**: Multi-gateway — each IB Gateway instance runs on its own port with isolated client IDs
**Connection**: Background reconnect loop with lazy initialization; tools gracefully handle offline gateway via `@cached_tool` stale responses
**Persistence**: SQLite for NLV history and drawdown tracking
**Security**: Tailscale Funnel (HTTPS + non-guessable URL); all tools are read-only (no order placement)

## Tools

32 tools across 9 modules:

| Module | Count | Tools |
|--------|------:|-------|
| **Account** | 5 | Account summary, margin analysis (efficiency/headroom/per-symbol), daily P&L ($ and % of NLV), multi-account consolidated view with FX conversion, account listing |
| **Briefing** | 3 | Unified briefing (positions, P&L, margin, movers, concentration — replaces 3 legacy tools), geopolitical risk scanner, thesis conformance checker |
| **Portfolio** | 1 | Positions with cost basis, unrealized P&L ($ and %), and portfolio weight |
| **Market Data** | 6 | Real-time quotes, historical OHLCV bars, contract details, dividends, contract search, technicals (SMA/RSI/MACD/Bollinger) |
| **Live Data** | 4 | FX rates, intraday bars, options chains with Greeks, cross-symbol performance comparison |
| **Risk** | 4 | What-if margin simulation, stress testing (custom scenarios + preflight), correlation matrix, Value-at-Risk (parametric) |
| **Intelligence** | 5 | Currency exposure, rebalance planning, sector decomposition, position deep-dive with P&L attribution, portfolio beta vs benchmark |
| **Monitoring** | 2 | Drawdown tracking from peak (reads historical NLV from SQLite), connection health diagnostics with event history |
| **Orders** | 2 | Trade history (fills/journal/gains/completed), open order status |

## Dashboard API

REST endpoints served alongside MCP on the same process via `@mcp.custom_route()`. Powers a Vite+React frontend (optional, served as static files if built).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/summary` | GET | Account summary JSON (NLV, margin, cushion, leverage) |
| `/api/positions` | GET | Positions with merged cross-account view |
| `/api/prices` | GET | Live prices from Yahoo Finance (symbols via query param) |
| `/api/margin` | GET | Margin analysis (markdown) |
| `/api/account-summary` | GET | Account summary (markdown) |
| `/api/account-pnl` | GET | Daily P&L (markdown) |
| `/api/currency` | GET | Currency exposure (markdown) |
| `/api/stress` | GET | Stress test scenarios (markdown) |
| `/api/what-if` | GET | What-if margin simulation (markdown) |
| `/api/trades` | GET | Trade history (markdown) |
| `/api/dividends` | GET | Dividend calendar (markdown) |
| `/api/technicals` | GET | Technical indicators (markdown) |
| `/api/status` | GET | Connection health (markdown) |
| `/api/health` | GET | Simple health check JSON |
| `/api/query` | POST | Natural language queries via Claude API passthrough |

## Stack

- `mcp` (FastMCP) — Model Context Protocol SDK with streamable HTTP
- `ib_insync` — IB TWS API, asyncio-native
- `uvicorn` — ASGI server for HTTP transport
- `nest_asyncio` — Nested event loops (ib_insync inside FastMCP's asyncio loop)
- `httpx` — Async HTTP client for Claude API passthrough
- `pydantic` — Input validation and tool schemas
- `yfinance` — Yahoo Finance prices for dashboard (MCP tools use IB market data)

## Demo

### Live Dashboard Artifact

Full portfolio dashboard generated from a single prompt on claude.ai — margin health, positions, P&L heatmap, sector concentration, and correlation matrix, all rendered as an interactive artifact.

<iframe src="https://claude.site/public/artifacts/ee62368f-5175-4bc0-aea7-d4399ccaf7d4/embed" title="IBKR Portfolio Dashboard" width="100%" height="800" frameborder="0" allow="clipboard-write" allowfullscreen></iframe>

### Screenshots

<img src="assets/demo-output.png" width="75%" />

*Technicals, sector exposure, and risk analysis from a single natural language prompt on claude.ai.*

<img src="assets/demo-toolchain.png" width="75%" />

*Chained tool calls across market data, portfolio, and intelligence modules.*

<img src="assets/demo-risk.png" width="75%" />

*Stress testing and margin analysis with automated scenario generation.*

