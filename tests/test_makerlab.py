import unittest
from makerlab.core import MarketSnapshot, Regime, RiskLimits, build_quotes, assess_risk, score_symbol
from makerlab.batch import MakerConfig, plan_batch
from makerlab.economics import estimate_rebate
from makerlab.live import ExchangeAdapter, LiveTradingBlocked

class TestMakerLab(unittest.TestCase):
 def snap(self,vol=.01): return MarketSnapshot('X',100,99.9,100.1,100000,100000,20000000,vol)
 def test_unknown(self): self.assertEqual(__import__('makerlab.core',fromlist=['classify_regime']).classify_regime([]),Regime.UNKNOWN)
 def test_shock_pauses(self): self.assertEqual(build_quotes(self.snap(.1),Regime.SHOCK,0).mode,'pause')
 def test_trend_one_sided(self):
  self.assertEqual(build_quotes(self.snap(),Regime.TREND_UP,0).mode,'one_sided_buy'); self.assertEqual(build_quotes(self.snap(),Regime.TREND_DOWN,0).mode,'one_sided_sell')
 def test_risk(self): self.assertFalse(assess_risk(self.snap(),Regime.RANGE,1001,0).allowed)
 def test_universe(self): self.assertFalse(score_symbol(MarketSnapshot('X',100,99,101,100000,100000,20000000,.01))['eligible'])
 def test_content_has_no_target_claim(self):
  from makerlab.core import make_market_note
  self.assertNotIn('目标价',make_market_note(self.snap(),Regime.RANGE,build_quotes(self.snap(),Regime.RANGE,0)))
 def test_batch_btc_eth(self):
  good=[MarketSnapshot('BTCUSDT',60000,59999,60001,1000000,1000000,2e9,.01),MarketSnapshot('ETHUSDT',3000,2999,3001,500000,500000,8e8,.01)]
  p=plan_batch(good,{'BTCUSDT':Regime.RANGE,'ETHUSDT':Regime.TREND_UP},config=MakerConfig(order_notional=100,max_orders_per_side=2))
  self.assertEqual(p.total_orders,6); self.assertEqual(p.plans[1].orders[0].position_side,'LONG')
 def test_inventory_uses_reduce_only_hedge_side(self):
  s=MarketSnapshot('BTCUSDT',60000,59999,60001,1000000,1000000,2e9,.01)
  p=plan_batch([s],{'BTCUSDT':Regime.RANGE},{'BTCUSDT':400},config=MakerConfig(order_notional=100,max_orders_per_side=1,max_net_notional=300))
  self.assertEqual(p.plans[0].orders[0].position_side,'LONG')
  self.assertTrue(p.plans[0].orders[1].reduce_only)
  self.assertEqual(p.plans[0].orders[1].position_side,'LONG')
 def test_rebate_requires_edge(self): self.assertTrue(estimate_rebate(100000,.5,.2).viable); self.assertFalse(estimate_rebate(100000,.5,.8).viable)
 def test_live_is_blocked(self):
  with self.assertRaises(LiveTradingBlocked): ExchangeAdapter().submit(None)
if __name__=='__main__': unittest.main()


