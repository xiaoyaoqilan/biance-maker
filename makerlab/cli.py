import json
import sys
sys.stdout.reconfigure(encoding='utf-8')
from dataclasses import asdict
from .batch import MakerConfig, plan_batch
from .core import MarketSnapshot, Regime, make_market_note, build_quotes
from .economics import estimate_rebate

def demo_report():
    snapshots = [
        MarketSnapshot('BTCUSDT',60000,59999,60001,1000000,950000,2000000000,.012),
        MarketSnapshot('ETHUSDT',3000,2999.5,3000.5,500000,480000,800000000,.018),
    ]
    regimes = {'BTCUSDT': Regime.RANGE, 'ETHUSDT': Regime.TREND_UP}
    plan = plan_batch(snapshots, regimes, config=MakerConfig(order_notional=100, max_orders_per_side=3))
    volume = sum(abs(o.price*o.quantity) for p in plan.plans for o in p.orders)
    rebate = estimate_rebate(volume, .5, .35)
    notes = [make_market_note(s, regimes[s.symbol], build_quotes(s, regimes[s.symbol], 0)) for s in snapshots]
    return {'mode':'simulation', 'plan':asdict(plan), 'rebate':asdict(rebate), 'notes':notes}

def main():
    print(json.dumps(demo_report(), ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
