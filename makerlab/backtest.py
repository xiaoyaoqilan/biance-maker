from dataclasses import dataclass
from .core import Candle, MarketSnapshot, Regime, build_quotes

@dataclass(frozen=True)
class BacktestResult:
    pnl: float
    fills: int
    final_inventory: float
    max_inventory: float
    skipped: int

def run_backtest(symbol: str, candles: list[Candle], initial_cash: float=10000.0) -> BacktestResult:
    cash, inventory, fills, skipped, max_inventory = initial_cash, 0.0, 0, 0, 0.0
    for c in candles:
        s = MarketSnapshot(symbol, c.close, c.close*.9999, c.close*1.0001, 100000, 100000, c.volume, max(.001,(c.high-c.low)/c.close))
        q = build_quotes(s, Regime.RANGE, inventory*c.close)
        if q.bid is None and q.ask is None: skipped += 1; continue
        if q.bid is not None and c.low <= q.bid and inventory < 1: inventory += 1; cash -= q.bid; fills += 1
        if q.ask is not None and c.high >= q.ask and inventory > -1: inventory -= 1; cash += q.ask; fills += 1
        max_inventory = max(max_inventory, abs(inventory))
    mark = candles[-1].close if candles else 0
    return BacktestResult(round(cash+inventory*mark-initial_cash,6), fills, inventory, max_inventory, skipped)
