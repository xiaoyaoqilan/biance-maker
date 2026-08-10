from dataclasses import dataclass

@dataclass(frozen=True)
class RebateReport:
    maker_volume: float
    gross_rebate: float
    adverse_selection: float
    funding_cost: float
    operational_cost: float
    net_estimate: float
    viable: bool

def estimate_rebate(maker_volume, rebate_bps, adverse_selection_bps, funding_cost=0.0, operational_cost=0.0):
    gross=maker_volume*rebate_bps/10000
    adverse=maker_volume*adverse_selection_bps/10000
    net=gross-adverse-funding_cost-operational_cost
    return RebateReport(maker_volume,round(gross,6),round(adverse,6),round(funding_cost,6),round(operational_cost,6),round(net,6),net>0)
