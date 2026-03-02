# Project: ibkr_mcp — Interactive Brokers MCP Server

## What This Is
An MCP (Model Context Protocol) server that connects to IBKR's TWS API via ib_insync,
exposing portfolio data, market data, margin analytics, and order management as tools
that Claude Code can call directly. Think: personal Bloomberg terminal you talk to in English.

## Who This Is For
Jeff Bai — independent trader, ~$26M CAD gross portfolio, concentrated semiconductors.
Two IBKR accounts (personal + 蛋总's). This runs on fragserv (headless 5900X/3090)
connected to IB Gateway 24/7.

## My Skill Level
Entry-level Python. I understand trading, financial concepts, and system architecture deeply.
I can read code and debug with guidance, but I'm not writing this from scratch.
Explain the "why" behind architectural decisions, not finance concepts.

## Tech Stack
- Python 3.11+
- FastMCP (from `mcp` package — `from mcp.server.fastmcp import FastMCP`)
- ib_insync 0.9.86 (or ib_async if we migrate later — API identical)
- Pydantic v2 for input validation
- Transport: stdio (Claude Code runs this as a subprocess)

## Architecture

```
ibkr-mcp/
├── CLAUDE.md              # This file — Claude Code instructions
├── README.md              # Human-readable docs
├── requirements.txt       # Python dependencies
├── config.py              # Connection settings, loaded from .env
├── .env.example           # Template for environment variables
├── .gitignore
│
├── server.py              # Entry point — FastMCP server with lifespan
│
├── tools/                 # MCP tool implementations (one file per domain)
│   ├── __init__.py
│   ├── account.py         # Account summary, margin, buying power
│   ├── portfolio.py       # Positions, P&L, holdings
│   ├── market_data.py     # Quotes, historical bars, contract details
│   ├── orders.py          # Open orders, executions, order placement
│   └── analytics.py       # What-if scenarios, concentration, risk metrics
│
├── core/                  # Shared infrastructure
│   ├── __init__.py
│   ├── connection.py      # IB connection manager (lifespan pattern)
│   ├── formatting.py      # Response formatting (JSON + Markdown)
│   └── errors.py          # Error handling utilities
│
└── tests/                 # Tests (add later)
    └── test_connection.py
```

## Critical Technical Decisions

### 1. Lifespan Pattern for IB Connection
ib_insync's IB() object must persist across tool calls. We use FastMCP's lifespan
context manager to connect on startup and disconnect on shutdown. Tools access
the IB instance through `ctx.request_context.lifespan_context`.

### 2. Event Loop Compatibility
ib_insync uses asyncio natively. `IB.connectAsync()` is a coroutine.
FastMCP also uses asyncio. They share the same event loop — no conflicts.
DO NOT use `ib.connect()` (blocking) — always use `await ib.connectAsync()`.

### 3. Read-Only First
Phase 1 is entirely read-only: portfolio, quotes, account data.
Order placement comes in Phase 2 with explicit safety annotations.
All read-only tools get: readOnlyHint=True, destructiveHint=False.

### 4. Decimal for Money
All financial values use Python's `Decimal` type internally.
Format to 2 decimal places for prices, 2 for percentages.
Never use float for money math.

### 5. Multi-Account Support
Jeff has multiple IBKR accounts. Tools should accept an optional `account` parameter.
Default to the primary account from config.

## Environment Variables (.env)

```
IB_HOST=127.0.0.1
IB_PORT=4001            # 4001 for Gateway, 7497 for TWS
IB_CLIENT_ID=10         # Unique per connection — don't use 0 (reserved for manual TWS)
IB_TIMEOUT=10
IB_READONLY=false       # Set true to disable order-related tools
PRIMARY_ACCOUNT=        # Main account ID (optional, auto-detected if one account)
```

## Tool Naming Convention
All tools prefixed with `ibkr_` to avoid conflicts with other MCP servers.
snake_case, action-oriented: `ibkr_get_positions`, `ibkr_get_quote`, etc.

## Current State
- [ ] Project scaffolded
- [ ] Lifespan connection pattern working
- [ ] Account tools implemented
- [ ] Portfolio tools implemented
- [ ] Market data tools implemented
- [ ] Analytics/what-if tools implemented
- [ ] Order tools (Phase 2)
- [ ] Tested against live Gateway

## Implementation Phases

### Phase 1: Foundation + Read-Only (BUILD THIS FIRST)
1. server.py with lifespan connecting to IB Gateway
2. core/connection.py — connection manager
3. tools/account.py — account summary, margin, buying power
4. tools/portfolio.py — positions, P&L
5. tools/market_data.py — quotes, historical bars
6. tools/analytics.py — what-if margin scenarios

### Phase 2: Orders + Risk (LATER)
7. tools/orders.py — open orders, executions, order placement
8. Enhanced analytics — stress tests, correlation

### Phase 3: Polish
9. Reconnection logic
10. Caching layer for frequently-requested data
11. Structured output types

## Rules for Claude Code

1. **Explain before implementing**: For any new file or architectural change,
   describe your approach and wait for approval.
2. **One tool domain at a time**: Implement account.py, test it, then portfolio.py, etc.
3. **Use the 4-part framework for complex concepts**:
   - ELI5 analogy
   - Technical concept name
   - The actual code with comments
   - Why this approach matters
4. **Keep files under 200 lines**. Split if they grow.
5. **All money math uses Decimal** — never float.
6. **All IB calls use async** — never blocking `ib.connect()` or `ib.sleep()`.
7. **Error handling on every IB call** — the API can timeout, disconnect, return None.
8. **Comment the "why"** not the "what".
9. **Don't add features I didn't ask for** — no auth, no caching, no logging frameworks yet.
10. **Test against live gateway** before moving to next tool domain.

## Don't Do This
- Don't use Django, FastAPI, or any web framework — this is MCP only
- Don't add a database — IB is the source of truth
- Don't add authentication — this runs locally on fragserv
- Don't add WebSockets or HTTP endpoints — stdio transport only
- Don't use the `ibapi` package directly — use ib_insync
- Don't optimize for multiple simultaneous users — single user only
- Don't add type:ignore comments — fix the types properly
