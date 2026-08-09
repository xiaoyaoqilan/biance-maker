from dataclasses import dataclass, asdict
from .core import RiskLimits, Regime, assess_risk, build_quotes, score_symbol

@dataclass(frozen=True)
class MakerConfig:
    symbols: tuple[str,...] = ('BTCUSDT','ETHUSDT')
    maker_rebate_bps: float = 0.5
    max_gross_notional: float = 5000.0
    max_net_notional: float = 1500.0
    max_orders_per_side: int = 3
    order_notional: float = 100.0
    tick_size: float = 0.01
    cooldown_seconds: int = 30

@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    position_side: str
    price: float
    quantity: float
    reduce_only: bool
    post_only: bool
    layer: int
    reason: str

@dataclass(frozen=True)
class SymbolPlan:
    symbol: str
    regime: str
    eligible: bool
    risk_mode: str
    quote: dict
    orders: tuple[OrderIntent,...]
    blockers: tuple[str,...]

@dataclass(frozen=True)
class BatchPlan:
    plans: tuple[SymbolPlan,...]
    gross_notional: float
    net_notional: float
    total_orders: int
    blocked: bool

def _signed_delta(order):
    """Return the signed change to net exposure in Hedge Mode."""
    if order.position_side == 'LONG': return order.price * order.quantity * (1 if order.side == 'BUY' else -1)
    return order.price * order.quantity * (-1 if order.side == 'SELL' else 1)

def _orders_for_quote(snapshot, quote, config, inventory_notional=0.0):
    orders=[]
    for level in range(config.max_orders_per_side):
        factor=1 + level*.35
        if quote.bid is not None:
            price=round((quote.bid*(1-level*quote.width_bps/20000*factor))/config.tick_size)*config.tick_size
            reducing_short = inventory_notional < -config.max_net_notional
            orders.append(OrderIntent(snapshot.symbol,'BUY','SHORT' if reducing_short else 'LONG',price,config.order_notional/price,reducing_short,True,level+1,'cover_short' if reducing_short else quote.reason))
        if quote.ask is not None:
            price=round((quote.ask*(1+level*quote.width_bps/20000*factor))/config.tick_size)*config.tick_size
            reducing_long = inventory_notional > config.max_net_notional
            orders.append(OrderIntent(snapshot.symbol,'SELL','LONG' if reducing_long else 'SHORT',price,config.order_notional/price,reducing_long,True,level+1,'reduce_long' if reducing_long else quote.reason))
    return tuple(orders)

def plan_batch(snapshots, regimes, inventories=None, limits=None, config=None):
    config=config or MakerConfig(); limits=limits or RiskLimits(); inventories=inventories or {}
    plans=[]; gross=net=0.0
    for snapshot in snapshots:
        if snapshot.symbol not in config.symbols: continue
        regime=regimes.get(snapshot.symbol,Regime.UNKNOWN); inv=inventories.get(snapshot.symbol,0.0)
        eligibility=score_symbol(snapshot); risk=assess_risk(snapshot,regime,inv,0.0,limits)
        quote=build_quotes(snapshot,regime,inv,tick_size=config.tick_size)
        blockers=list(eligibility['reasons'])+list(risk.reasons)
        allowed=eligibility['eligible'] and risk.allowed and quote.mode!='pause'
        orders=_orders_for_quote(snapshot,quote,config,inv) if allowed else tuple()
        gross += sum(abs(o.price*o.quantity) for o in orders)
        net += sum(_signed_delta(o) for o in orders)
        plans.append(SymbolPlan(snapshot.symbol,regime.value,allowed,risk.mode,asdict(quote),orders,tuple(blockers)))
    blocked=gross>config.max_gross_notional or abs(net)>config.max_net_notional
    if blocked:
        plans=[SymbolPlan(p.symbol,p.regime,False,'pause',p.quote,tuple(),p.blockers+('global_exposure_limit',)) for p in plans]
    return BatchPlan(tuple(plans),round(gross,6),round(net,6),sum(len(p.orders) for p in plans),blocked)
