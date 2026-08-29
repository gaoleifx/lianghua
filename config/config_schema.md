# v6 策略配置说明

- `data`：本地数据库目录、财务披露保守滞后、历史数据长度与时效要求。默认目录为 `D:\股票数据库`，可用 `GOLDMINER_DATA_ROOT` 覆盖。
- `universe`：中证800及沪深300+中证500降级方案、上市时间和成交额过滤。
- `factors`：点时量价、相对强度、突破、趋势效率和下行风险权重；权重必须合计为1。财务因子当前禁用。
- `portfolio`：持仓数量、单股/行业上限、现金、持有期和每日调仓检查时间；自然周最多成功调仓一次。
- `risk`：成本/ATR止损、移动止盈、大盘趋势与组合回撤分级降仓。
- `execution`：保护限价、滑点、费用、订单超时和A股整手数量。
- `state`：状态目录环境变量，默认写入数据库目录下的 `strategy_state`。

敏感配置仅通过环境变量提供：`GM_TOKEN`、`GOLDMINER_BACKTEST_ACCOUNT`、`GOLDMINER_LIVE_ACCOUNT`。源码不保存凭据。

## v6 潜力股字段

- `factors.weights`：`relative_strength`、`trend_acceleration`、`breakout`、`volume_confirmation`、`trend_efficiency`、`downside_risk`、`liquidity`。
- `factors.weak_market_weight_threshold`：市场宽度低于该值时使用弱市权重；`factors.weak_market_weights`：与主权重同键且合计为1，默认提高下行风险和适度突破的权重。
- `factors.minimum_potential_confirmations`：新买入所需的最少潜力确认数。
- `factors.entry_percentile` / `exit_percentile`：新买排名门槛与持仓退出缓冲分别配置；`weak_market_exit_percentile` 与 `normal_market_exit_percentile` 允许按市场宽度在弱市严格退出、正常市场扩大持有缓冲。
- `factors.require_benchmark_bullish_for_new_entries`：可选的弱指数趋势硬门禁；默认关闭时由市场宽度决定目标暴露，弱市仍可低仓位试仓。
- `factors.potential_transition_relaxed_enabled`：允许满足近期相对强度改善和风险边界的弱转强股票进入评分池；不是无条件放宽动量过滤。
- `factors.potential_transition_early_weight`：潜力因子中早期强度的权重；其余权重分配给近期相对基准改善，默认 `0.55`。
- `deployment.live_new_entries_enabled`：独立的实盘新买入人工门禁；回测失败时必须为 `false`。
- `deployment.validation_status`：最近一次无未来数据验证状态。
- `risk.permanent_capital_lock`：达到资本/高水位硬线后是否永久停止新增风险。

本地日线接口使用严格 `< signal_date` 查询；财务库修复并显式启用前，任何财务字段不得进入交易评分。
