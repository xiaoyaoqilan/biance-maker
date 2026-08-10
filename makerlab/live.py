class LiveTradingBlocked(Exception):
    pass

class ExchangeAdapter:
    """Explicit boundary for a future exchange connector.

    This MVP never sends orders. A production adapter must implement exchange
    filters, user-stream reconciliation, partial fills, STP, rate limiting,
    cancel/replace safety and a kill switch before enabling writes.
    """
    def __init__(self, enabled=False): self.enabled=enabled
    def submit(self, order_intent):
        if not self.enabled: raise LiveTradingBlocked('live trading is disabled; use simulation or testnet')
        raise NotImplementedError('live adapter is intentionally not implemented')
