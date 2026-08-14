from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional


class Regime(str, Enum):
    RANGE='range'; TREND_UP='trend_up'; TREND_DOWN='trend_down'; SHOCK='shock'; UNKNOWN='unknown'


@dataclass(frozen=True)
class Candle:
    timestamp:int; open:float; high:float; low:float; close:float; volume:float=0.0


@dataclass(frozen=True)
class MarketSnapshot:
    symbol:str; mid:float; bid:float; ask:float; depth_bid_notional:float; depth_ask_notional:float; volume_24h:float; volatility:float; funding_rate:float=0.0; status:str='TRADING'
    @property
    def spread_bps(self): return max(0.0,(self.ask-self.bid)/self.mid*10000) if self.mid else float('inf')
    @property
    def depth_imbalance(self):
        total=self.depth_bid_notional+self.depth_ask_notional
        return (self.depth_bid_notional-self.depth_ask_notional)/total if total else 0.0


@dataclass(frozen=True)
class Quote:
    symbol:str; mode:str; bid:Optional[float]; ask:Optional[float]; width_bps:float; inventory_skew_bps:float; reason:str


@dataclass(frozen=True)
class RiskLimits:
    max_inventory_notional:float=1000.0; max_daily_loss:float=100.0; max_volatility:float=.08; max_funding_rate:float=.003


@dataclass(frozen=True)
class RiskDecision:
    allowed:bool; mode:str; reasons:tuple[str,...]


def validate_snapshot(s: MarketSnapshot) -> tuple[str,...]:
    """Return deterministic data-quality blockers for one market snapshot."""
    issues=[]
    for name, value in (
        ('mid', s.mid), ('bid', s.bid), ('ask', s.ask),
        ('depth_bid_notional', s.depth_bid_notional),
        ('depth_ask_notional', s.depth_ask_notional),
        ('volume_24h', s.volume_24h), ('volatility', s.volatility),
        ('funding_rate', s.funding_rate),
    ):
        if not math.isfinite(value):
            issues.append(f'{name}_not_finite')
    if s.mid <= 0: issues.append('mid_not_positive')
    if s.bid <= 0: issues.append('bid_not_positive')
    if s.ask <= 0: issues.append('ask_not_positive')
    if s.bid > s.ask: issues.append('crossed_book')
    if s.depth_bid_notional < 0: issues.append('negative_bid_depth')
    if s.depth_ask_notional < 0: issues.append('negative_ask_depth')
    if s.volume_24h < 0: issues.append('negative_volume')
    if s.volatility < 0: issues.append('negative_volatility')
    return tuple(dict.fromkeys(issues))


def _ema(values, period):
    if not values:return 0.0
    a=2/(period+1); x=values[0]
    for v in values[1:]:x=a*v+(1-a)*x
    return x


def _atr(candles, period=14):
    r=candles[-period:]; prev=r[0].open; tr=[]
    for c in r:
        tr.append(max(c.high-c.low,abs(c.high-prev),abs(c.low-prev))); prev=c.close
    return (sum(tr)/len(tr))/r[-1].close if r[-1].close else 0.0


def classify_regime(candles, shock_atr_pct=.06):
    if len(candles)<30:return Regime.UNKNOWN
    closes=[c.close for c in candles]; atr=_atr(candles)
    if atr>=shock_atr_pct:return Regime.SHOCK
    fast,slow=_ema(closes[-40:],12),_ema(closes[-40:],26); slope=(closes[-1]-closes[-10])/closes[-10]
    hi=max(c.high for c in candles[-20:-1]); lo=min(c.low for c in candles[-20:-1]); t=max(.002,atr*.35)
    if fast>slow and slope>t and closes[-1]>=hi*.995:return Regime.TREND_UP
    if fast<slow and slope<-t and closes[-1]<=lo*1.005:return Regime.TREND_DOWN
    return Regime.RANGE


def build_quotes(s, regime, inventory_notional, base_width_bps=6.0, tick_size=.01, volatility_multiplier=.18, max_width_bps=30.0, inventory_limit=1000.0):
    if validate_snapshot(s):return Quote(s.symbol,'pause',None,None,0,0,'invalid_market_snapshot')
    if tick_size <= 0 or not math.isfinite(tick_size):return Quote(s.symbol,'pause',None,None,0,0,'invalid_tick_size')
    if regime in (Regime.UNKNOWN,Regime.SHOCK):return Quote(s.symbol,'pause',None,None,0,0,'insufficient_or_shock_market')
    raw_width=max(base_width_bps,s.spread_bps+s.volatility*10000*volatility_multiplier); width=min(max_width_bps,raw_width)
    inventory_ratio=max(-1.0,min(1.0,inventory_notional/max(1.0,inventory_limit))); skew=inventory_ratio*width*1.2
    micro_bias=s.depth_imbalance*s.spread_bps*.25; center=s.mid*(1+micro_bias/10000)
    bid=round(center*(1-(width+skew)/20000)/tick_size)*tick_size; ask=round(center*(1+(width-skew)/20000)/tick_size)*tick_size
    if bid <= 0 or ask <= 0 or bid >= ask:
        return Quote(s.symbol,'pause',None,None,0,0,'invalid_rounded_quote')
    if regime==Regime.TREND_UP:return Quote(s.symbol,'one_sided_buy',bid,None,round(width,2),round(skew,2),'trend_up_only_bid')
    if regime==Regime.TREND_DOWN:return Quote(s.symbol,'one_sided_sell',None,ask,round(width,2),round(skew,2),'trend_down_only_ask')
    return Quote(s.symbol,'two_sided',bid,ask,round(width,2),round(skew,2),'range_inventory_skew')


def assess_risk(s, regime, inventory_notional, daily_pnl, limits=RiskLimits()):
    reasons=[]
    reasons.extend(validate_snapshot(s))
    if s.status!='TRADING':reasons.append('symbol_not_trading')
    if abs(inventory_notional)>=limits.max_inventory_notional:reasons.append('inventory_limit')
    if daily_pnl<=-abs(limits.max_daily_loss):reasons.append('daily_loss_limit')
    if s.volatility>=limits.max_volatility:reasons.append('volatility_limit')
    if abs(s.funding_rate)>=limits.max_funding_rate:reasons.append('funding_limit')
    if regime==Regime.SHOCK:reasons.append('shock_regime')
    reasons=tuple(dict.fromkeys(reasons))
    return RiskDecision(not reasons,'reduce_only' if reasons else 'quote',reasons)


def score_symbol(s,min_volume=10_000_000,max_spread_bps=12.0):
    reasons=list(validate_snapshot(s))
    if s.status!='TRADING':reasons.append('not_trading')
    if s.volume_24h<min_volume:reasons.append('low_volume')
    if s.spread_bps>max_spread_bps:reasons.append('wide_spread')
    if min(s.depth_bid_notional,s.depth_ask_notional)<=0:reasons.append('missing_depth')
    if abs(s.funding_rate)>.003:reasons.append('funding_extreme')
    depth=min(s.depth_bid_notional,s.depth_ask_notional); score=100*(.55*min(1,depth/100000)+.45*max(0,1-s.spread_bps/max_spread_bps))
    return {'symbol':s.symbol,'score':round(score,2),'eligible':not reasons,'reasons':tuple(dict.fromkeys(reasons))}


def make_market_note(s,regime,quote):
    return f'{s.symbol} 当前状态：{regime.value}。盘口价差约 {s.spread_bps:.1f} bps，波动率 {s.volatility*100:.2f}%。系统动作：{quote.mode}，原因：{quote.reason}。这不是买卖建议。'
