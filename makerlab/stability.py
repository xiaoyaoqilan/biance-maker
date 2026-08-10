from dataclasses import dataclass

@dataclass(frozen=True)
class QuoteState:
    mid: float
    regime: str
    created_at: float

def should_refresh(previous, mid, regime, now, price_move_bps=4.0, max_age_seconds=30):
    if previous is None:return True
    if previous.regime != regime:return True
    if previous.mid and abs(mid-previous.mid)/previous.mid*10000 >= price_move_bps:return True
    return now-previous.created_at >= max_age_seconds
