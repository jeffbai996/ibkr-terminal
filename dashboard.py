"""
Dashboard REST API — custom routes added to the MCP server.

Registers /api/* endpoints via @mcp.custom_route() so they're served
from the same process/port as the MCP protocol. The frontend calls
these directly instead of routing through the Anthropic API.

Must be imported AFTER server_http.py swaps app.mcp and registers tools.
"""

import json
import logging
import math
import time
from decimal import Decimal
from typing import Optional

import httpx
import yfinance as yf
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import mcp
from config import (
    IB_HOST, IB_PORT, PRIMARY_ACCOUNT, SECONDARY_ACCOUNT,
)
from core.formatting import get_decimal, safe_decimal

# Tool functions and input models
from tools.account import (
    ibkr_margin, ibkr_get_account_summary, ibkr_get_account_pnl,
    MarginInput, AccountInput,
)
from tools.intelligence import ibkr_currency, CurrencyInput
from tools.risk import ibkr_stress_test, ibkr_what_if, StressTestInput, WhatIfInput
from tools.orders import ibkr_trades, TradesInput
from tools.market_data import ibkr_dividends, ibkr_technicals, DividendInput, TechnicalsInput
from tools.monitoring import ibkr_connection_status

logger = logging.getLogger("ibkr_mcp.dashboard")

# Dashboard-specific config (from env)
import os
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MCP_URL = os.environ.get("MCP_URL", "")
YAHOO_CACHE_TTL = int(os.environ.get("YAHOO_CACHE_TTL", "30"))

# Yahoo Finance price cache: symbol -> (data, timestamp)
_price_cache: dict[str, tuple[dict, float]] = {}


# ── Helpers ──────────────────────────────────────────────────────────

class _FakeRequestContext:
    def __init__(self, lc: dict):
        self.lifespan_context = lc


class _FakeContext:
    """Shim that satisfies ctx.request_context.lifespan_context access."""
    def __init__(self, lc: dict):
        self.request_context = _FakeRequestContext(lc)


async def _ensure_connected_and_ctx() -> _FakeContext:
    """
    Get a fake MCP context wrapping the current IB connections.

    Triggers a connection if the server hasn't connected yet (REST route
    hit before the first MCP session). Uses the same lock and globals
    as server_http.py's http_lifespan.
    """
    import server_http as sh

    async with sh._ib_lock:
        needs_connect = (
            sh._ib is None
            or not sh._ib.isConnected()
            or not sh._health.connected
        )
        if needs_connect:
            sh._ib, sh._primary_account = await sh._connect_ib()
            sh._account_map = {}
            sh._health_map = {}
            for acc in sh._ib.managedAccounts():
                sh._account_map[acc] = sh._ib
                sh._health_map[acc] = sh._health

            # Secondary
            if sh.IB_PORT_2:
                sh._ib2, sh._secondary_account = await sh._connect_ib2()
                if sh._ib2:
                    for acc in sh._ib2.managedAccounts():
                        sh._account_map[acc] = sh._ib2
                        sh._health_map[acc] = sh._health2

    return _FakeContext({
        "ib": sh._ib,
        "ib2": sh._ib2,
        "primary_account": sh._primary_account,
        "secondary_account": sh._secondary_account,
        "account_map": sh._account_map,
        "health": sh._health,
        "health_map": sh._health_map,
    })


def _get_ib(account: Optional[str] = None):
    """Get IB instance from module globals (non-async, for data routes)."""
    import server_http as sh
    if account and account in sh._account_map:
        return sh._account_map[account]
    return sh._ib


def _get_accounts() -> list[str]:
    import server_http as sh
    accounts: list[str] = []
    if sh._ib is not None and sh._ib.isConnected():
        accounts.extend(sh._ib.managedAccounts())
    if sh._ib2 is not None and sh._ib2.isConnected():
        for acc in sh._ib2.managedAccounts():
            if acc not in accounts:
                accounts.append(acc)
    return accounts


def _to_float(d: Optional[Decimal]) -> Optional[float]:
    return float(d) if d is not None else None


# ── Structured Data Endpoints ────────────────────────────────────────

def _account_summary_json(ib, account: str) -> dict:
    """Extract key account metrics as JSON-friendly dict."""
    try:
        summary = ib.accountSummary(account)
    except Exception as e:
        logger.warning(f"accountSummary failed for {account}: {e}")
        return {"account": account, "error": str(e)}
    if not summary:
        return {"account": account, "error": "No summary available"}

    vals = {item.tag: (item.value, item.currency) for item in summary}

    nlv = get_decimal(vals, "NetLiquidation")
    gpv = get_decimal(vals, "GrossPositionValue")
    init_margin = get_decimal(vals, "InitMarginReq")
    maint_margin = get_decimal(vals, "MaintMarginReq")
    excess_init = get_decimal(vals, "ExcessLiquidity")
    cushion_raw = get_decimal(vals, "Cushion")
    currency = vals.get("NetLiquidation", (None, "USD"))[1]

    return {
        "account": account,
        "currency": currency,
        "nlv": _to_float(nlv),
        "gpv": _to_float(gpv),
        "cash": _to_float(get_decimal(vals, "TotalCashValue")),
        "buying_power": _to_float(get_decimal(vals, "BuyingPower")),
        "init_margin": _to_float(init_margin),
        "maint_margin": _to_float(maint_margin),
        "excess_liquidity": _to_float(excess_init),
        "full_excess_liquidity": _to_float(get_decimal(vals, "FullExcessLiquidity")),
        "cushion_pct": float(cushion_raw * 100) if cushion_raw else None,
        "leverage": float(gpv / nlv) if nlv and nlv != 0 else None,
        "margin_util_pct": float(init_margin / nlv * 100) if nlv and nlv != 0 else None,
    }


def _positions_json(ib, account: str) -> list[dict]:
    """Extract positions as JSON-friendly list."""
    try:
        items = list(ib.portfolio(account))
    except Exception as e:
        logger.warning(f"portfolio() failed for {account}: {e}")
        return []
    if not items:
        return []

    total_value = sum(abs(Decimal(str(p.marketValue))) for p in items)
    items.sort(key=lambda p: abs(p.marketValue), reverse=True)

    return [
        {
            "symbol": p.contract.symbol,
            "sec_type": p.contract.secType,
            "shares": p.position,
            "avg_cost": p.averageCost,
            "market_price": p.marketPrice,
            "market_value": p.marketValue,
            "unrealized_pnl": p.unrealizedPNL if not math.isnan(p.unrealizedPNL) else None,
            "currency": p.contract.currency,
            "weight_pct": round(
                float(Decimal(str(abs(p.marketValue))) / total_value * 100), 2
            ) if total_value else 0.0,
            "account": account,
        }
        for p in items
    ]


@mcp.custom_route("/api/summary", methods=["GET"])
async def api_summary(request: Request) -> JSONResponse:
    """Account summary — structured JSON for NLV banner and margin bars."""
    account = request.query_params.get("account")

    if account:
        ib = _get_ib(account)
        return JSONResponse({"accounts": [_account_summary_json(ib, account)]})

    accounts = _get_accounts()
    results = []
    combined_nlv = 0.0
    currency = "CAD"

    for acc in accounts:
        ib = _get_ib(acc)
        data = _account_summary_json(ib, acc)
        results.append(data)
        if data.get("nlv") is not None:
            combined_nlv += data["nlv"]
        if data.get("currency"):
            currency = data["currency"]

    return JSONResponse({
        "accounts": results,
        "combined_nlv": combined_nlv,
        "currency": currency,
    })


@mcp.custom_route("/api/positions", methods=["GET"])
async def api_positions(request: Request) -> JSONResponse:
    """Portfolio positions — structured JSON for tables."""
    account = request.query_params.get("account")

    if account:
        ib = _get_ib(account)
        return JSONResponse({"positions": _positions_json(ib, account)})

    all_positions = []
    for acc in _get_accounts():
        all_positions.extend(_positions_json(_get_ib(acc), acc))

    # Merged view (same symbol across accounts)
    merged: dict[str, dict] = {}
    for p in all_positions:
        key = f"{p['symbol']}_{p['currency']}"
        if key not in merged:
            merged[key] = {"symbol": p["symbol"], "currency": p["currency"],
                           "shares": 0, "market_value": 0.0, "unrealized_pnl": 0.0}
        merged[key]["shares"] += p["shares"]
        merged[key]["market_value"] += p["market_value"]
        if p["unrealized_pnl"] is not None:
            merged[key]["unrealized_pnl"] += p["unrealized_pnl"]

    merged_list = sorted(merged.values(), key=lambda x: abs(x["market_value"]), reverse=True)
    return JSONResponse({"positions": all_positions, "merged": merged_list})


# ── Yahoo Finance Prices ─────────────────────────────────────────────

def _fetch_yahoo_quote(symbol: str) -> dict:
    now = time.time()
    cached = _price_cache.get(symbol)
    if cached and (now - cached[1]) < YAHOO_CACHE_TTL:
        return cached[0]

    try:
        info = yf.Ticker(symbol).fast_info
        data = {
            "symbol": symbol,
            "price": getattr(info, "last_price", None),
            "previous_close": getattr(info, "previous_close", None),
            "open": getattr(info, "open", None),
            "day_high": getattr(info, "day_high", None),
            "day_low": getattr(info, "day_low", None),
            "currency": getattr(info, "currency", "USD"),
            "timestamp": now,
        }
        if data["price"] and data["previous_close"]:
            data["change"] = data["price"] - data["previous_close"]
            data["change_pct"] = data["change"] / data["previous_close"] * 100
        else:
            data["change"] = data["change_pct"] = None
        _price_cache[symbol] = (data, now)
        return data
    except Exception as e:
        logger.warning(f"Yahoo Finance error for {symbol}: {e}")
        return {"symbol": symbol, "error": str(e), "timestamp": now}


@mcp.custom_route("/api/prices", methods=["GET"])
async def api_prices(request: Request) -> JSONResponse:
    """Live prices from Yahoo Finance (ibkr_quote has no market data sub)."""
    raw = request.query_params.get("symbols", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    results = {sym: _fetch_yahoo_quote(sym) for sym in symbols}
    return JSONResponse({"prices": results, "source": "yahoo_finance"})


# ── Tool Proxy Routes (return markdown) ──────────────────────────────

@mcp.custom_route("/api/margin", methods=["GET"])
async def api_margin(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = MarginInput(
        detail=request.query_params.get("detail", "summary"),
        symbol=request.query_params.get("symbol"),
        account=request.query_params.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_margin(params, ctx)})


@mcp.custom_route("/api/account-summary", methods=["GET"])
async def api_account_summary_md(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = AccountInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_get_account_summary(params, ctx)})


@mcp.custom_route("/api/account-pnl", methods=["GET"])
async def api_account_pnl(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = AccountInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_get_account_pnl(params, ctx)})


@mcp.custom_route("/api/currency", methods=["GET"])
async def api_currency(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    params = CurrencyInput(account=request.query_params.get("account"))
    return JSONResponse({"markdown": await ibkr_currency(params, ctx)})


@mcp.custom_route("/api/stress", methods=["GET"])
async def api_stress(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    dd = qp.get("drawdown_pct")
    params = StressTestInput(
        scenario=qp.get("scenario", "preflight"),
        drawdown_pct=float(dd) if dd else None,
        sigma_multiplier=float(qp.get("sigma_multiplier", "2.0")),
        account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_stress_test(params, ctx)})


@mcp.custom_route("/api/what-if", methods=["GET"])
async def api_what_if(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = WhatIfInput(
        action=qp["action"], symbol=qp["symbol"],
        quantity=int(qp["quantity"]), account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_what_if(params, ctx)})


@mcp.custom_route("/api/trades", methods=["GET"])
async def api_trades(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = TradesInput(
        view=qp.get("view", "fills"),
        account=qp.get("account"),
        symbol_filter=qp.get("symbol_filter"),
    )
    return JSONResponse({"markdown": await ibkr_trades(params, ctx)})


@mcp.custom_route("/api/dividends", methods=["GET"])
async def api_dividends(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = DividendInput(
        scope=qp.get("scope", "calendar"),
        symbol=qp.get("symbol"),
        account=qp.get("account"),
    )
    return JSONResponse({"markdown": await ibkr_dividends(params, ctx)})


@mcp.custom_route("/api/technicals", methods=["GET"])
async def api_technicals(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    qp = request.query_params
    params = TechnicalsInput(symbol=qp["symbol"], sections=qp.get("sections", "all"))
    return JSONResponse({"markdown": await ibkr_technicals(params, ctx)})


@mcp.custom_route("/api/status", methods=["GET"])
async def api_status(request: Request) -> JSONResponse:
    ctx = await _ensure_connected_and_ctx()
    return JSONResponse({"markdown": await ibkr_connection_status(ctx)})


@mcp.custom_route("/api/health", methods=["GET"])
async def api_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "accounts": _get_accounts()})


# ── Claude API Passthrough (Query tab only) ──────────────────────────

SYSTEM_PROMPT = (
    "You are a portfolio data assistant connected to IBKR via MCP.\n"
    "RULES:\n"
    "- Do NOT use ibkr_quote — no market data subscription.\n"
    "- When asked for multiple accounts, call each tool for each account in the SAME turn.\n"
    "- Return raw tool output. Minimal commentary."
)


@mcp.custom_route("/api/query", methods=["POST"])
async def api_query(request: Request) -> JSONResponse:
    """Claude API passthrough for the natural language Query tab."""
    if not ANTHROPIC_API_KEY:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not configured"})
    if not MCP_URL:
        return JSONResponse({"error": "MCP_URL not configured"})

    body = await request.json()
    prompt = body.get("prompt", "")
    system = body.get("system") or SYSTEM_PROMPT

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4000,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                    "mcp_servers": [{"type": "url", "url": MCP_URL, "name": "ibkr"}],
                },
            )
            data = resp.json()

        if "error" in data:
            return JSONResponse({"error": data["error"].get("message", str(data["error"]))})

        texts, tool_results = [], []
        for block in data.get("content", []):
            if block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
            if block.get("type") == "mcp_tool_result":
                tc = next((c["text"] for c in block.get("content", []) if c.get("type") == "text"), None)
                if tc:
                    tool_results.append(tc)

        return JSONResponse({
            "texts": texts,
            "tool_results": tool_results,
            "response": "\n\n".join(texts) or "\n\n".join(tool_results) or "No response.",
        })

    except httpx.TimeoutException:
        return JSONResponse({"error": "Request timed out (60s). Try a simpler query."})
    except Exception as e:
        logger.error(f"Query error: {e}")
        return JSONResponse({"error": str(e)})
