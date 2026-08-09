import json
import sys
sys.stdout.reconfigure(encoding='utf-8')
from makerlab.backtest import run_backtest
from makerlab.core import Candle, MarketSnapshot, build_quotes, classify_regime, make_market_note, score_symbol, assess_risk

def main():
    candles = [Candle(i, 100+i*.03, 100.2+i*.03, 99.8+i*.03, 100+i*.03, 1000+i) for i in range(40)]
    regime = classify_regime(candles)
    snapshot = MarketSnapshot('DEMOUSDT', candles[-1].close, candles[-1].close-.02, candles[-1].close+.02, 250000, 240000, 50000000, .01)
    quote = build_quotes(snapshot, regime, 0)
    result = {'backtest': run_backtest('DEMOUSDT', candles).__dict__, 'universe': score_symbol(snapshot), 'regime': regime.value, 'risk': assess_risk(snapshot, regime, 0, 0).__dict__, 'quote': quote.__dict__, 'content': make_market_note(snapshot, regime, quote)}
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()

