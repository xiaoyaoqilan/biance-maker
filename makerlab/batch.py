from dataclasses import dataclass, asdict
import math
from .core import RiskLimits, Regime, assess_risk, build_quotes, score_symbol


@dataclass(frozen=True)
class MakerConfig:
    symbols: tuple[str,...] = ('BTCUSDT','ETHUSDT')
    maker_rebate_bps: float = 0.5
    max_gross_notional: float = 5000.0
    max_net_notional: float = 1500.0
    max_orders_per_side: int = 2
    max_total_orders: int = 12
    order_notional: float = 100.0
    tick_size: float = 0.01
    min_expected_edge_bps: float = 0.25
    max_quote_width_bps: float = 30.0
    cooldown_seconds: int = 30

    def __post_init__(self):
        if not self.symbols: raise ValueError('symbols must not be empty')
        if any(not symbol for symbol in self.symbols): raise ValueError('symbols must be non-empty')
        for name in ('maker_rebate_bps','max_gross_notional','max_net_notional','order_notional','tick_size','min_expected_edge_bps','max_quote_width_bps'):
            value=getattr(self,name)
            if not math.isfinite(value) or value < 0: raise ValueError(f'{name} must be finite and non-negative')
        if self.order_notional <= 0 or self.tick_size <= 0: raise ValueError('order_notional and tick_size must be positive')
        if self.max_orders_per_side < 1 or self.max_total_orders < 1: raise ValueError('order limits must be positive')
        if self.cooldown_seconds < 0: raise ValueError('cooldown_seconds must be non-negative')


@dataclass(frozen=True)
class OrderIntent:
    symbol: str; side: str; position_side: str; price: float; quantity: float; reduce_only: bool; post_only: bool; layer: int; reason: str


@dataclass(frozen=True)
class SymbolPlan:
    symbol: str; regime: str; eligible: bool; risk_mode: str; quote: dict; orders: tuple[OrderIntent,...]; blockers: tuple[str,...]


@dataclass(frozen=True)
class BatchPlan:
    plans: tuple[SymbolPlan,...]; gross_notional: float; net_notional: float; total_orders: int; blocked: bool
    requested_gross_notional: float = 0.0
    requested_net_notional: float = 0.0
    requested_orders: int = 0
    blockers: tuple[str,...] = ()


def _signed_delta(order):
    if order.position_side == 'LONG': return order.price*order.quantity*(1 if order.side=='BUY' else -1)
    return order.price*order.quantity*(-1 if order.side=='SELL' else 1)


def _orders_for_quote(snapshot, quote, config, inventory_notional=0.0, reduce_threshold=None):
    orders=[]
    reduce_threshold = config.max_net_notional if reduce_threshold is None else reduce_threshold
    for level in range(config.max_orders_per_side):
        factor=1+level*.35
        if quote.bid is not None:
            price=round((quote.bid*(1-level*quote.width_bps/20000*factor))/config.tick_size)*config.tick_size
            reducing_short=inventory_notional < -reduce_threshold
            orders.append(OrderIntent(snapshot.symbol,'BUY','SHORT' if reducing_short else 'LONG',price,config.order_notional/price,reducing_short,True,level+1,'cover_short' if reducing_short else quote.reason))
        if quote.ask is not None:
            price=round((quote.ask*(1+level*quote.width_bps/20000*factor))/config.tick_size)*config.tick_size
            reducing_long=inventory_notional > reduce_threshold
            orders.append(OrderIntent(snapshot.symbol,'SELL','LONG' if reducing_long else 'SHORT',price,config.order_notional/price,reducing_long,True,level+1,'reduce_long' if reducing_long else quote.reason))
    return tuple(orders)


def plan_batch(snapshots, regimes, inventories=None, limits=None, config=None, daily_pnl=0.0):
    config=config or MakerConfig(); limits=limits or RiskLimits(); inventories=inventories or {}; plans=[]; gross=net=0.0
    reduce_threshold=min(config.max_net_notional, limits.max_inventory_notional)
    for snapshot in snapshots:
        if snapshot.symbol not in config.symbols: continue
        regime=regimes.get(snapshot.symbol,Regime.UNKNOWN); inv=inventories.get(snapshot.symbol,0.0); eligibility=score_symbol(snapshot); risk=assess_risk(snapshot,regime,inv,daily_pnl,limits)
        quote=build_quotes(snapshot,regime,inv,tick_size=config.tick_size,max_width_bps=config.max_quote_width_bps,inventory_limit=limits.max_inventory_notional)
        blockers=list(eligibility['reasons'])+list(risk.reasons)
        expected_edge=quote.width_bps*.5+config.maker_rebate_bps-snapshot.volatility*10000*.03
        if expected_edge < config.min_expected_edge_bps: blockers.append('edge_below_threshold')
        allowed=eligibility['eligible'] and risk.allowed and quote.mode!='pause' and 'edge_below_threshold' not in blockers
        orders=_orders_for_quote(snapshot,quote,config,inv,reduce_threshold) if allowed else tuple(); gross+=sum(abs(o.price*o.quantity) for o in orders); net+=sum(_signed_delta(o) for o in orders)
        plans.append(SymbolPlan(snapshot.symbol,regime.value,allowed,risk.mode,asdict(quote),orders,tuple(dict.fromkeys(blockers))))
    total=sum(len(p.orders) for p in plans); requested_gross=round(gross,6); requested_net=round(net,6); requested_orders=total
    global_blockers=[]
    if gross>config.max_gross_notional: global_blockers.append('gross_notional_limit')
    if abs(net)>config.max_net_notional: global_blockers.append('net_notional_limit')
    if total>config.max_total_orders: global_blockers.append('total_order_limit')
    blocked=bool(global_blockers)
    if blocked:
        plans=[SymbolPlan(p.symbol,p.regime,False,'pause',p.quote,tuple(),p.blockers+('global_exposure_or_order_limit',)) for p in plans]
    executable_gross=0.0 if blocked else requested_gross
    executable_net=0.0 if blocked else requested_net
    executable_orders=0 if blocked else requested_orders
    return BatchPlan(tuple(plans),executable_gross,executable_net,executable_orders,blocked,requested_gross,requested_net,requested_orders,tuple(global_blockers))
