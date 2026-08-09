# BTC/ETH Maker Rebate Assistant

面向做市商的 BTC/ETH 双品种批量 Maker 做市辅助工具。

它解决的问题不是“制造虚假成交量”，而是帮助做市商在 Hedge Mode 下管理 BTC/ETH 的双边被动报价、库存和返佣经济性：

- 自动生成 LONG / SHORT 两套报价意图
- 震荡行情进行双边报价
- 单边行情减少逆势开仓
- 库存过多时切换到 reduce-only 减仓逻辑
- 只输出 Post-Only 订单意图，避免意外变成 taker
- 估算 maker rebate 是否覆盖逆向选择、资金费率和运营成本
- 批量管理 BTCUSDT 与 ETHUSDT，而不是手工操作单个交易对

## 工作流

```text
行情数据
  -> BTC/ETH 筛选
  -> 震荡 / 上涨 / 下跌 / 极端行情识别
  -> Hedge Mode 多空库存计算
  -> Post-Only 多层报价
  -> 返佣净收益估算
  -> 全局仓位与风险拦截
  -> 模拟报告 / 后续交易所适配器
```

## 快速运行

```powershell
cd C:\Users\朱忠伟\Documents\Codex\2026-08-09\https-github-com-xiaoyaoqilan-biance-maker\work\repo
python -m unittest discover -s tests -v
python -m makerlab.cli
```

演示报告会展示：

- BTC/ETH 是否通过交易对筛选
- 当前报价是双边还是单边
- LONG / SHORT 订单意图
- 哪些订单是开仓，哪些订单是 reduce-only
- 全局 gross/net exposure
- maker rebate、逆向选择和净收益估算
- 可用于 Binance Square 的事实型内容草稿

## 策略规则

### 震荡行情

BTC/ETH 都可以进行有限的双边报价；报价宽度随盘口价差和波动率变化，库存偏离时自动调整报价中心。

### 上涨行情

减少 SHORT 开仓，只保留有限的 LONG 被动买单；已有空头优先回补。

### 下跌行情

减少 LONG 开仓，只保留有限的 SHORT 被动卖单；已有多头优先减仓。

### 极端行情

暂停新报价，只保留 reduce-only 风控动作，并进入冷却期。

## 返佣经济模型

```text
净收益 = maker rebate
       - 逆向选择损失
       - 资金费率
       - 滑点
       - 运营成本
```

只有净收益估算为正时，系统才会把策略标记为值得继续研究。负手续费、返佣资格和做市计划以交易所当期规则及账户等级为准，本项目不保证任何费率或收益。

## 实盘边界

当前版本默认 `simulation`，不会真实下单。`makerlab/live.py` 会明确阻止未实现的实盘适配器。

接入真实交易所前，还必须完成：

- exchangeInfo 的 tick size、lot size 和最小名义价值校验
- 用户数据流和订单状态对账
- 部分成交、超时但状态未知、断线恢复
- Hedge Mode 的 positionSide 与 reduceOnly 校验
- STP 防自成交
- API 订单限频和退避
- 全局保证金、日亏损和 kill switch

旧版交易脚本和 Binance Square 流水线保留在仓库中，但不再作为新的做市策略核心。