"""
Shared test fixtures for ibkr_mcp integration tests.

Provides mock IB connection, mock MCP Context, and factory functions
for constructing ib_insync data types with realistic test data.
"""

import asyncio
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ib_insync import Stock
from ib_insync.contract import ContractDetails
from ib_insync.objects import AccountValue, BarData, PnL, PortfolioItem
from ib_insync.order import OrderState


# --- Constants ---

TEST_ACCOUNT = "U1234567"


# --- Data Factories ---

def make_account_value(tag: str, value: str, currency: str = "USD") -> AccountValue:
    """Build an AccountValue like IB returns from accountSummary()."""
    return AccountValue(
        account=TEST_ACCOUNT, tag=tag, value=value,
        currency=currency, modelCode="",
    )


def make_summary(overrides: dict | None = None) -> list[AccountValue]:
    """Build a full accountSummary() response with sensible defaults.

    Mimics a ~$10M leveraged portfolio: 1.8x leverage, healthy cushion.
    Pass overrides dict to replace specific tags.
    """
    defaults = {
        "NetLiquidation": ("10000000", "USD"),
        "GrossPositionValue": ("18000000", "USD"),
        "TotalCashValue": ("-8000000", "USD"),
        "BuyingPower": ("2000000", "USD"),
        "InitMarginReq": ("6000000", "USD"),
        "MaintMarginReq": ("4500000", "USD"),
        "ExcessLiquidity": ("4000000", "USD"),
        "FullExcessLiquidity": ("5500000", "USD"),
        "Cushion": ("0.55", "USD"),
        "SMA": ("3000000", "USD"),
        "EquityWithLoanValue": ("10000000", "USD"),
    }
    if overrides:
        defaults.update(overrides)

    return [
        make_account_value(tag, val, ccy)
        for tag, (val, ccy) in defaults.items()
    ]


def make_portfolio_item(
    symbol: str,
    position: float,
    market_price: float,
    market_value: float,
    avg_cost: float,
    unrealized_pnl: float,
    realized_pnl: float = 0.0,
    currency: str = "USD",
) -> PortfolioItem:
    """Build a PortfolioItem like IB returns from portfolio()."""
    contract = Stock(symbol, "SMART", currency)
    return PortfolioItem(
        contract=contract, position=position, marketPrice=market_price,
        marketValue=market_value, averageCost=avg_cost,
        unrealizedPNL=unrealized_pnl, realizedPNL=realized_pnl,
        account=TEST_ACCOUNT,
    )


def make_positions() -> list[PortfolioItem]:
    """Build a realistic 5-position semiconductor portfolio."""
    return [
        make_portfolio_item("NVDA", 5000, 140.0, 700000.0, 100.0, 200000.0),
        make_portfolio_item("MU", 10000, 95.0, 950000.0, 80.0, 150000.0),
        make_portfolio_item("AVGO", 1000, 180.0, 180000.0, 150.0, 30000.0),
        make_portfolio_item("TSM", 3000, 110.0, 330000.0, 90.0, 60000.0),
        make_portfolio_item("SGOV", 20000, 100.25, 2005000.0, 100.0, 5000.0),
    ]


def make_bar(
    dt: date, o: float, h: float, lo: float, c: float, vol: int,
) -> BarData:
    """Build a BarData like IB returns from reqHistoricalDataAsync()."""
    return BarData(
        date=dt, open=o, high=h, low=lo, close=c,
        volume=vol, average=(o + h + lo + c) / 4, barCount=100,
    )


def make_order_state(
    init_margin_change: str = "-50000",
    maint_margin_change: str = "-37500",
    equity_change: str = "0",
) -> OrderState:
    """Build an OrderState like IB returns from whatIfOrder().

    Note: all margin fields are STRINGS, not floats.
    """
    return OrderState(
        status="PreSubmitted",
        initMarginBefore="6000000",
        maintMarginBefore="4500000",
        equityWithLoanBefore="10000000",
        initMarginChange=init_margin_change,
        maintMarginChange=maint_margin_change,
        equityWithLoanChange=equity_change,
        initMarginAfter="5950000",
        maintMarginAfter="4462500",
        equityWithLoanAfter="10000000",
    )


# --- Mock IB ---

def make_mock_ib(
    summary: list[AccountValue] | None = None,
    positions: list[PortfolioItem] | None = None,
    accounts: list[str] | None = None,
) -> MagicMock:
    """Build a mock IB object that returns test data for sync methods.

    Async methods (qualifyContractsAsync, reqHistoricalDataAsync, etc.)
    must be configured per-test since their return values vary.
    """
    ib = MagicMock()
    ib.accountSummary.return_value = make_summary() if summary is None else summary
    ib.portfolio.return_value = make_positions() if positions is None else positions
    ib.managedAccounts.return_value = [TEST_ACCOUNT] if accounts is None else accounts

    # Async methods default to empty — override in tests
    ib.qualifyContractsAsync = AsyncMock(return_value=[Stock("TEST", "SMART", "USD")])
    ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
    ib.reqContractDetailsAsync = AsyncMock(return_value=[])

    return ib


# --- Mock MCP Context ---

def make_ctx(ib: MagicMock | None = None, primary_account: str = TEST_ACCOUNT):
    """Build a mock MCP Context with IB in the lifespan context."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "ib": ib or make_mock_ib(),
        "primary_account": primary_account,
    }
    return ctx


# --- Auto-patch asyncio.sleep so tests don't wait ---

@pytest.fixture(autouse=True)
def _fast_sleep():
    """Replace asyncio.sleep with a no-op for all tests."""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield
