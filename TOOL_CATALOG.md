# ibkr_mcp — Tool Catalog & Design Document

## Overview

This document defines every MCP tool exposed by ibkr_mcp. Each tool maps to one or more
ib_insync API calls, with clear input/output schemas and safety annotations.

Claude Code reads this to understand what's available and how to use it.

---

## Tool Categories

| Category | File | Tools | Phase |
|----------|------|-------|-------|
| Account | tools/account.py | 4 tools | 1 |
| Portfolio | tools/portfolio.py | 4 tools | 1 |
| Market Data | tools/market_data.py | 4 tools | 1 |
| Analytics | tools/analytics.py | 4 tools | 1 |
| Orders | tools/orders.py | 5 tools | 2 |

---

## Category 1: Account (tools/account.py)

### `ibkr_get_account_summary`
**Purpose**: Full account overview — NAV, margin, buying power, cushion.
**IB API**: `ib.accountSummary()` or `ib.accountValues()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `account` (Optional[str]): Account ID. Default: primary from config.
**Output**: JSON/Markdown with:
- Net Liquidation Value (NLV)
- Gross Position Value
- Total Cash Value (by currency)
- Available Funds / Buying Power
- Initial Margin Requirement
- Maintenance Margin Requirement
- Excess Liquidity
- Cushion (maintenance margin buffer %)
- SMA (Special Memorandum Account)
- Leverage ratio (gross / NLV)

### `ibkr_get_margin_summary`
**Purpose**: Focused margin view — the numbers that matter during stress.
**IB API**: `ib.accountSummary()` filtered to margin fields
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `account` (Optional[str])
**Output**:
- Initial Margin vs Equity → distance to margin call
- Maintenance Margin vs Equity → distance to forced liquidation
- Excess Liquidity (initial and maintenance)
- Cushion percentage
- Available buying power by currency

### `ibkr_list_accounts`
**Purpose**: List all managed accounts connected to this session.
**IB API**: `ib.managedAccounts()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**: None
**Output**: List of account IDs

### `ibkr_get_account_pnl`
**Purpose**: Real-time daily P&L for the account.
**IB API**: `ib.reqPnL(account, modelCode)`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `account` (Optional[str])
**Output**:
- Daily P&L
- Unrealized P&L
- Realized P&L

---

## Category 2: Portfolio (tools/portfolio.py)

### `ibkr_get_positions`
**Purpose**: All current positions with market value and P&L.
**IB API**: `ib.portfolio()` (returns PortfolioItem objects with live P&L)
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `account` (Optional[str])
- `symbol_filter` (Optional[str]): Filter by symbol substring (e.g., "MU")
**Output**: Per position:
- Symbol, SecType, Exchange, Currency
- Shares/Quantity
- Average Cost
- Market Price
- Market Value
- Unrealized P&L (absolute and %)
- Realized P&L

### `ibkr_get_position_detail`
**Purpose**: Deep dive on a single position — contract details + P&L.
**IB API**: `ib.portfolio()` filtered + `ib.reqPnLSingle()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `symbol` (str): Stock symbol
- `account` (Optional[str])
**Output**:
- Everything from get_positions for this symbol
- ConId (contract ID)
- Daily P&L
- Unrealized P&L
- Value as % of portfolio

### `ibkr_get_portfolio_snapshot`
**Purpose**: High-level portfolio summary — the "at a glance" view.
**IB API**: Combines `ib.portfolio()` + `ib.accountSummary()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `account` (Optional[str])
**Output**:
- Total NAV
- Total Market Value
- Total Unrealized P&L (absolute and %)
- Total Daily P&L
- Margin utilization %
- Top 5 positions by weight
- Concentration metrics (top position %, HHI)
- Currency breakdown (CAD vs USD exposure)

### `ibkr_get_portfolio_by_currency`
**Purpose**: Positions grouped by currency — critical for CAD/USD split.
**IB API**: `ib.portfolio()` grouped by currency
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `account` (Optional[str])
**Output**:
- USD positions: symbols, values, weights
- CAD positions: symbols, values, weights
- Other currencies if any
- Currency totals and percentages

---

## Category 3: Market Data (tools/market_data.py)

### `ibkr_get_quote`
**Purpose**: Current market data for a symbol.
**IB API**: `ib.reqMktData()` or `ib.reqTickers()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `symbol` (str): Stock symbol (e.g., "MU", "NVDA")
- `sec_type` (str): Default "STK". Options: STK, OPT, FUT, CASH, etc.
- `exchange` (str): Default "SMART"
- `currency` (str): Default "USD"
**Output**:
- Last price, bid, ask, spread
- Volume
- Open, High, Low, Close
- Change (absolute and %)
- Timestamp of last update

### `ibkr_get_historical_bars`
**Purpose**: Historical OHLCV data.
**IB API**: `ib.reqHistoricalData()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `symbol` (str)
- `duration` (str): e.g., "30 D", "1 Y", "6 M"
- `bar_size` (str): e.g., "1 day", "1 hour", "5 mins"
- `what_to_show` (str): Default "TRADES". Options: MIDPOINT, BID, ASK, etc.
- `use_rth` (bool): Regular trading hours only. Default True.
- `sec_type` (str): Default "STK"
- `exchange` (str): Default "SMART"
- `currency` (str): Default "USD"
**Output**:
- List of bars: date, open, high, low, close, volume, barCount, average
- Summary stats: period high, period low, avg volume

### `ibkr_get_contract_details`
**Purpose**: Full contract specification for a symbol.
**IB API**: `ib.reqContractDetails()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `symbol` (str)
- `sec_type` (str): Default "STK"
- `exchange` (str): Default "SMART"
- `currency` (str): Default "USD"
**Output**:
- ConId, Symbol, SecType, Exchange
- Long name
- Industry, Category, Subcategory
- Market cap tier
- Min tick
- Trading hours
- Valid exchanges

### `ibkr_get_options_chain`
**Purpose**: Options chain for a symbol.
**IB API**: `ib.reqSecDefOptParams()` + `ib.qualifyContracts()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `symbol` (str)
- `expiration` (Optional[str]): Filter to specific expiry (YYYYMMDD)
- `right` (Optional[str]): "C" for calls, "P" for puts
- `strike_range` (Optional[tuple]): Min/max strike filter
**Output**:
- Available expirations
- Available strikes
- For each contract: strike, expiry, right, bid, ask, last, IV, delta, gamma, theta, vega

---

## Category 4: Analytics (tools/analytics.py)

### `ibkr_what_if_sell`
**Purpose**: Margin impact simulation — "what happens to my margin if I sell X?"
**IB API**: `ib.whatIfOrder()` — returns margin impact without placing the order
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `symbol` (str)
- `quantity` (int): Number of shares to simulate selling
- `account` (Optional[str])
**Output**:
- Current margin state (initial, maintenance, equity)
- Post-trade margin state (initial, maintenance, equity)
- Initial margin relief (delta)
- Maintenance margin relief (delta)
- Change in excess liquidity
- Change in buying power
- Estimated proceeds

### `ibkr_what_if_buy`
**Purpose**: Same as sell but for adding to positions.
**IB API**: `ib.whatIfOrder()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `symbol` (str)
- `quantity` (int)
- `account` (Optional[str])
**Output**: Same structure as what_if_sell

### `ibkr_portfolio_concentration`
**Purpose**: Concentration risk analysis.
**IB API**: Calculated from `ib.portfolio()` + `ib.accountSummary()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `account` (Optional[str])
**Output**:
- Position weights (% of NLV)
- HHI (Herfindahl-Hirschman Index)
- Top 1/3/5 concentration
- Sector concentration (if classifiable)
- Single-name risk flags (>20% in one name)

### `ibkr_margin_stress_test`
**Purpose**: "What happens if the portfolio drops X%?"
**IB API**: Calculated from current positions + margin requirements
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `drawdown_pct` (float): Simulated portfolio drawdown (e.g., 5.0 for 5%)
- `account` (Optional[str])
**Output**:
- Current state: NLV, margin req, excess liquidity, cushion
- Post-stress state: estimated NLV, margin req, excess liquidity, cushion
- Distance to margin call (initial)
- Distance to forced liquidation (maintenance)
- Max drawdown before margin call
- Max drawdown before forced liquidation

---

## Category 5: Orders (tools/orders.py) — PHASE 2

### `ibkr_get_open_orders`
**Purpose**: All currently open/working orders.
**IB API**: `ib.openOrders()` + `ib.openTrades()`
**Annotations**: readOnly=True, destructive=False, idempotent=True

### `ibkr_get_executions`
**Purpose**: Recent fills/executions.
**IB API**: `ib.executions()`
**Annotations**: readOnly=True, destructive=False, idempotent=True
**Input**:
- `days_back` (int): Default 1. How many days of executions to fetch.
- `symbol_filter` (Optional[str])

### `ibkr_place_order`
**Purpose**: Place an order. REQUIRES EXPLICIT CONFIRMATION.
**IB API**: `ib.placeOrder()`
**Annotations**: readOnly=False, destructive=True, idempotent=False
**Input**:
- `symbol` (str)
- `action` (str): "BUY" or "SELL"
- `quantity` (int)
- `order_type` (str): "LMT", "MKT", "STP", etc.
- `limit_price` (Optional[Decimal])
- `time_in_force` (str): "GTC", "DAY", "IOC", etc.
- `account` (Optional[str])
- `confirm` (bool): Must be True to execute. Safety latch.
**Output**:
- Order ID
- Order status
- Trade object details

### `ibkr_cancel_order`
**Purpose**: Cancel a working order.
**IB API**: `ib.cancelOrder()`
**Annotations**: readOnly=False, destructive=True, idempotent=True

### `ibkr_cancel_all_orders`
**Purpose**: Cancel ALL working orders. Nuclear option.
**IB API**: `ib.reqGlobalCancel()`
**Annotations**: readOnly=False, destructive=True, idempotent=True
**Input**:
- `confirm` (bool): Must be True. Double safety latch.

---

## Response Format Design

All tools support dual output:
- **Markdown** (default): Human-readable, optimized for Claude's context window
- **JSON**: Machine-readable, for when Claude needs to do calculations

The `response_format` parameter controls this. Default is markdown because Claude
Code's primary consumer is Jeff reading natural language.

### Markdown Formatting Rules
- Prices: `$123.45` (2 decimal places)
- Percentages: `12.34%` (2 decimal places)
- Large numbers: `$1,234,567.89` (comma-separated)
- P&L colors described in text: "(+$1,234.56)" or "(-$567.89)"
- Timestamps: Pacific time displayed, UTC stored
- Currency always shown: "$123.45 USD" or "$456.78 CAD"

---

## Error Handling Strategy

Every tool call wraps IB API calls in try/except:

1. **ConnectionError**: IB Gateway not running → "Error: Not connected to IB Gateway. Check that Gateway is running on {host}:{port}"
2. **TimeoutError**: Request took too long → "Error: Request timed out after {timeout}s. IB Gateway may be overloaded."
3. **ValueError**: Bad input → "Error: Invalid {field}. Expected {constraint}."
4. **IB API errors**: Pass through IB's error message with context
5. **General**: "Error: Unexpected error — {type}: {message}"

Never let an exception propagate unhandled. Always return a useful error string.

---

## IB Gateway Connection Details

- **Host**: Usually 127.0.0.1 (localhost on fragserv)
- **Port**: 4001 (IB Gateway default) or 7497 (TWS default)
- **Client ID**: Must be unique. Use 10+ to avoid conflicts with other tools.
- **Read-only mode**: Disables order placement at the API level
- **Auto-sync**: ib_insync automatically syncs positions, orders, account data on connect

---

## Usage Examples (What Jeff Will Say to Claude Code)

These are the natural language queries this server enables:

- "Show me my portfolio" → `ibkr_get_portfolio_snapshot`
- "What's MU trading at?" → `ibkr_get_quote(symbol="MU")`
- "What happens to my margin if I sell 1000 MU?" → `ibkr_what_if_sell(symbol="MU", quantity=1000)`
- "Show me 蛋总's positions" → `ibkr_get_positions(account="U12345")`
- "How much buying power do I have?" → `ibkr_get_account_summary`
- "What's my distance to margin call?" → `ibkr_get_margin_summary`
- "If the market drops 5%, am I safe?" → `ibkr_margin_stress_test(drawdown_pct=5.0)`
- "Show me NVDA's last 30 days" → `ibkr_get_historical_bars(symbol="NVDA", duration="30 D")`
- "Compare position sizes across both accounts" → multiple calls with different account params
- "What's my CAD vs USD exposure?" → `ibkr_get_portfolio_by_currency`
