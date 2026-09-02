# 输入、输出与日志契约

规则时点：`as_of=2026-09-01`。

本契约使盘前计划可复核、可重算。缺失字段必须列入 `missing_inputs`；不得凭常识补价格、行情、费用、账户余额或实时竞价数据。

## 输入字段

### 1. 运行元数据

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `run_id` | string | 是 | 本次计划的唯一标识。 |
| `as_of` | date | 是 | 规则时点；当前文档固定为 `2026-09-01`。 |
| `decision_for` | date | 是 | 计划对应交易日；传给端到端脚本时字段名为 `decision_date`。 |
| `latest_completed_trading_date` | date | 是 | 最近一个已完成交易日，必须严格早于 `decision_for`；端到端脚本以此作为历史截止日。 |
| `decision_time` | datetime | 是 | 带 `+08:00` 的决策时间。精确股数端点仅接受计划日 09:25:00–09:29:59；更早阶段只能输出无股数的条件观察。 |
| `timezone` | string | 是 | A 股场景固定记录 `Asia/Shanghai`。 |
| `data_cutoff` | datetime | 是 | 输入数据的最晚时间，不得晚于 `decision_time`；精确股数端点要求为计划日 09:25:00 或之后。 |
| `live_data_available` | boolean | 是 | 只有确实接入并读取实时源时才为 `true`；本地计算脚本始终不提供实时数据。 |
| `trading_session_confirmed` | boolean | 精确股数时是 | 调用方查验适用交易所官方交易日历与临时休市通知后才能严格填 `true`。脚本直接拒绝周末，但无法离线独立识别全部节假日或临时休市。 |
| `rule_version_checked_at` | date | 是 | 交易规则最近一次官方核验日期。精确端点要求等于 `decision_date`，且计划日不得早于脚本的 `bundled_rules_as_of`。 |
| `rules_match_bundled_configuration` | boolean | 精确股数时是 | 只能在当天官方核验结果与脚本输出的 `bundled_rules_as_of` 配置一致时严格填 `true`；这是调用方声明，离线脚本不会独立联网验证。 |

### 2. 账户状态

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `equity` | number | 是 | 当前账户净资产。 |
| `cash` | number | 是 | 本次计算的现金基数，不包含未确认资金；若券商数值已扣除某笔委托，不得再把同一笔计入 `open_order_exposure`。 |
| `day_start_equity` | number | 是 | 计算当日损失率的基准，已按当日净外部现金流调整。 |
| `week_high_equity` | number | 是 | 本自然交易周截至当前的同口径权益高点，已排除出入金影响。 |
| `daily_loss_fraction` | number | 是 | 亏损用正数，例如 1.5% 写为 `0.015`。 |
| `weekly_loss_fraction` | number | 是 | 从本周权益高点回撤的正数比例。 |
| `stop_loss_count` | integer | 是 | 当日已执行或应执行的止损次数。 |
| `weekly_lock_remainder_of_week` | boolean | 是 | 是否仍处于已触发周锁的本周余下时间；必须跨日保存。 |
| `weekly_recovery_days_remaining` | integer | 是 | 周锁结束后尚余的半风险交易日，范围 0–5；跨周不能自动清零。 |
| `existing_portfolio_risk` | number | 是 | 已有持仓和待成交计划占用的含成本计划风险金额。 |
| `existing_total_notional` | number | 是 | 已有持仓与待成交计划占用的最坏成交名义金额。 |
| `existing_symbol_notional` | number | 是 | 当前候选同一证券已有仓位占用的名义金额；盈利加仓也不得漏记。 |
| `existing_symbol_stress_fraction` | number | 是 | 当前候选同一证券已有仓位占用的账户压力比例；必须与 `existing_symbol_notional * limit_down_fraction / equity` 一致。 |
| `existing_cluster_stress_fraction` | number | 是 | 候选所属相关组已经占用的账户压力比例；必须包含同证券压力，因此不得小于 `existing_symbol_stress_fraction`。 |
| `existing_ice_trial_plan_count` | integer | 是 | 当日已经保留的冰点试错计划数。冰点试错状态下只允许一个，数值大于 0 时拒绝第二个计划；每评估一只后必须刷新，不能复用旧快照。 |
| `open_positions` | array | 是 | 每个持仓的代码、股数、参考价、止损、题材、板块、计划风险和压力值。无持仓传空数组。 |
| `open_order_exposure` | number | 是 | 尚可能成交的未决委托现金占用。只要大于 0，端到端脚本输出 `OPEN_ORDER_EXPOSURE_UNRESOLVED`，不再给另一笔精确新仓股数；先确认成交/撤单并刷新全部账户占用。 |

已有持仓的每一项还必须包含 `sellable_shares`、`locked_shares`、`acquired_trade_date`、`logic_invalidation_price`、`correlation_group`、盘前固定的 `flat_open_tolerance` 和 `opening_scenario`；任何卖出计划都以 `sellable_shares` 为上限，开盘后不得改写平开容差。

### 3. 三轴环境

| 字段 | 允许值 | 必填 |
|---|---|---:|
| `trend_score` | `[-100,+100]` | 是 |
| `breadth_score` | `[-100,+100]` | 是 |
| `liquidity_score` | `[-100,+100]` | 是 |
| `short_sentiment_score` | `[-100,+100]` | 是 |
| `index_score` | `[-100,+100]` | 是 |
| `index_close`、`index_high_120d`、`index_low_120d` | number | 震荡时必填 |
| `range_position_120d` | number | 震荡时必填，保留未四舍五入原值 |
| `index_state` | `rising`、`high_range`、`falling`、`low_range`、`unknown` | 是 |
| `index_turning_up` | boolean | 是；仅低位震荡且有转强证据时为 `true` |
| `sentiment_state` | `icepoint`、`repair`、`main_rise`、`climax`、`strong_divergence`、`retreat`、`icepoint_repair`、`unknown` | 是 |
| `style_state` | `low_first_board`、`board_relay`、`capacity_trend`、`twenty_cm`、`large_cap`、`mixed`、`unknown` | 是 |
| `mainline_state` | `confirmed`、`low_resonance`、`mixed`、`collapsed`、`unknown` | 是，作为独立开仓硬门 |

每个状态必须配套：

```text
axis_evidence[] = {
  axis, metric, value, unit, observed_at,
  source, source_url_or_id, evidence_grade, notes
}
```

指数轴必须记录四个原始分项、加权总分、120 日区间原始值和 `index_turning_up` 证据。情绪轴至少说明昨日涨停溢价、炸板率变化和高位股反馈是否已知；风格轴必须先于候选模型判定。主线硬门另记核心股状态与板块广度。未知可以保留，不能填造数值。

### 4. 候选模型

| 字段 | 类型/允许值 | 必填 | 说明 |
|---|---|---:|---|
| `setup` | `low_first_board` 或 `mainline_pullback` | 是 | 两类历史完全分开。 |
| `symbol`、`name` | string | 是 | 证券标识。 |
| `exchange` | `SSE` 或 `SZSE` | 是 | 其他市场直接 `OUT_OF_SCOPE`。 |
| `market_segment` | `main_board`、`chinext`、`star` | 是 | 仅作一致性复核；脚本先从规范六位 `symbol` 与 `exchange` 派生板块，再据此固定单股上限和申报网格。两者冲突直接拒绝。 |
| `correlation_group` | string | 是 | 同题材、同核心或同事件使用相同值。 |
| `is_mainline_core` | boolean/unknown | 是 | 必须有证据。 |
| `prior_board_count` | integer/unknown | 首板模型必填 | `low_first_board` 必须确认前一日为首板而非二板以上。 |
| `location_evidence` | object | 首板模型必填 | 说明“低位”的参照区间、时间窗和证据，不接受只写“看起来低”。 |
| `pullback_structure` | object | 回踩模型必填 | 支撑、核心地位、缩量/承接和失效条件。 |
| `entry_price` | number | 是 | 盘前冻结的最坏允许入场价；精确计划必须符合 0.01 元价格最小变动单位。09:25 后脚本以 `effective_entry_price=max(entry_price, final_auction_price)` 重算，禁止沿用更低旧价。 |
| `stop_price` | number | 是 | 预计可执行的结构失效价，不能只给任意百分比；必须符合 0.01 元价格最小变动单位。 |
| `target_price` | number | 是 | 第一个明确兑现压力位，不得为满足 R 倒推；必须符合 0.01 元价格最小变动单位。 |
| `final_auction_price` | number | 精确股数时是 | 计划日 09:25 最终集合竞价成交价，必须来自带时间戳的真实快照并符合 0.01 元价格网格。 |
| `forbidden_chase_price` | number | 是 | 盘前先固定的禁止追价线，必须符合 0.01 元价格网格；最坏允许入场价不得高于它，最终竞价价越过它直接取消。 |
| `limit_down_fraction` | number | 是 | 供审计的普通股票一档跌停压力跌幅；在当前范围与规则时点必须匹配 `main_board=0.10`、`chinext/star=0.20`，不能低报。 |
| `sector_notional_cap` | number | 是 | 该题材/相关风险簇剩余的名义仓位上限。 |
| `minimum_order_shares` | integer | 脚本输出 | `main_board/chinext` 为 100，`star` 为 200；随规则版本核验，不由调用者覆盖。 |
| `share_increment` | integer | 脚本输出 | `main_board/chinext` 为 100，`star` 达最低数量后为 1；随规则版本核验。 |
| `sector_state` | `rising`、`high_range`、`falling`、`low_range`、`unknown` | 是 | `falling` 直接否决，`unknown` 最高只能条件观察。 |
| `sector_relative_strength` | object | 是 | 对基准指数和同类板块的相对强弱、口径、时点与证据。 |
| `sector_breadth` | object | 是 | 上涨/下跌家数、核心/前排扩散或其他既定广度口径。 |
| `sector_liquidity` | object | 是 | 成交额、相对量能及口径。 |
| `sector_core_feedback` | object | 是 | 核心竞价、开盘与负反馈状态。 |
| `stock_role` | `core`、`front_row`、`follower`、`unknown` | 是 | 排序固定为核心、前排、跟风；未知不能入选。 |

范围排除字段也必须显式提供：`security_type`、`is_st`、`is_delisting_period`、`is_ipo_unlimited_period`、`is_b_share`、`is_bse`、`is_etf`、`is_convertible_bond`、`is_margin_trade`。任一排除项为真，输出 `OUT_OF_SCOPE`。

风格适配字段：`style_setup_match` 取 `matched`、`mixed`、`mismatched` 或 `unknown`，并附 `style_setup_evidence`。不得为了适配而修改 `setup`。

### 5. 竞价快照

```text
auction = {
  observed_at,
  auction_phase,
  final_auction_data_confirmed,
  final_auction_price,
  indicative_price,
  indicative_gap_fraction,
  virtual_matched_quantity,
  virtual_unmatched_quantity,
  unmatched_side,
  core_auction_state,
  sector_breadth_state,
  source,
  evidence_grade
}
```

没有真实快照时，数值填 `null`、状态填 `unknown`，`live_data_available=false`、`final_auction_data_confirmed=false`。禁止把昨日收盘数据描述成今日竞价；这种情况下不得调用端到端精确股数入口。

### 6. 成本与历史

成本字段：

- `commission_rate`
- `minimum_commission`
- `commission_basis`：`all_in` 或 `net`
- `additional_fee_rate_per_side`
- `sell_tax_rate`
- `slippage_rate_per_side`

所有成本字段都必须显式提供；未知时不得输出精确股数。端到端精确计划的边界为 `minimum_commission>=5` 元/边、`commission_rate<=0.003`、`sell_tax_rate>=0.0005`、`slippage_rate_per_side>=0.0005`，并要求 `commission_rate + additional_fee_rate_per_side >= 0.0000641`。费率合计下限覆盖证管费、经手费和过户费；若实际参数与当前普通 A 股边界冲突，先核验佣金口径或更新规则配置，不输出精确股数。该边界只对应本文 `as_of=2026-09-01` 的普通 A 股范围；官方规则变化时先更新 Skill，不得靠极小正数或低报输入绕过。`additional_fee_rate_per_side` 只承载未包含在券商佣金中的过户费、经手费、证管费等双边附加费。若 `commission_basis=all_in` 且全佣已经包含这些费用，该字段必须为 `0`；若 `commission_basis=net`，该字段必须为尚未包含的正费率；禁止重复计算。

历史记录按模型分别传入：

```text
trade_history[] = {
  trade_id, exit_date, exit_timestamp, setup, symbol, exchange,
  trading_session_confirmed=true, trading_session_evidence_id,
  closed=true, costs_included=true,
  planned_entry_price, planned_stop_price, planned_exit_price,
  planned_slippage_rate_per_side,
  entry_price, exit_price, shares,
  gross_pnl_cash, commission_cash, tax_cash,
  additional_fees_cash, slippage_cash,
  commission_rate, minimum_commission, commission_basis,
  additional_fee_rate_per_side, sell_tax_rate,
  net_pnl, planned_risk_cash, net_r, followed_plan,
  setup_equity_before, setup_equity_after,
  exit_reason, evidence_log_id
}
```

`entry_price/exit_price` 是实际成交均价，`slippage_cash` 是计划价相对实际成交价的归因值，已经体现在实际成交价形成的 `gross_pnl_cash` 中，不得再次从净盈亏扣除。脚本会按实际买卖成交额和该行费率配置重算佣金、卖出税和附加费；再以计划入场、计划止损、计划压力滑点、股数和费用重算 `planned_risk_cash`，其中历史计划压力滑点只接受 `0.0005` 至 `0.05`，佣金率不得超过 `0.003`。升级统计使用重算分母而不是自由填写的 R；盈利采用实际配置与最低法定摩擦两次重算中较大风险分母，亏损采用两者中较小的风险分母，避免只给亏损行填大滑点或费率稀释亏损 R。脚本还会核对成交价与毛盈亏、费用与净盈亏、净 R、计划价与滑点归因、净盈亏与独立权益变化；容差只用于舍入对账，不得叠加后改变盈亏符号或把真实零期望、负期望变成正期望。

同一模型记录必须带规范六位 `symbol`、与代码匹配的 `exchange`，并由脚本确认属于当前普通 A 股范围；ETF、可转债或其他范围外证券不能用于解锁风险档。每行还必须严格声明 `trading_session_confirmed=true` 并提供非空 `trading_session_evidence_id`；脚本拒绝周末，并只接受 09:25 开盘撮合、09:30–11:30、13:00–15:00 的带 `+08:00` 平仓时间。节假日和临时休市仍由证据 ID 对应的外部官方日历负责，离线脚本不会自行联网确认。`exit_timestamp` 必须严格递增；`exit_date` 必须与时间戳日期一致，`trade_id` 唯一，前一笔 `setup_equity_after` 与后一笔 `setup_equity_before` 连续。这样同日交易不能靠重排列表压低最大回撤。`evaluate_setup_tier` 必须传 `history_cutoff_date`；任何晚于截止日的 `exit_date` 直接拒绝。盘前精确计划用 `latest_completed_trading_date` 而不是 `decision_date` 作为截止日，因此同日记录也不能提档。每笔费用现金必须与当时记录的费率、最低收费及实际成交额在 0.01 元容差内一致；没有成本配置、计划止损或计划压力滑点，不得用毛 R 或自报 R 代替净 R。没有同模型历史，不得给升级档。升级统计必须同时输出 `setup_trade_count`、`net_expectancy_r`、`discipline_execution_rate` 和 `setup_max_drawdown_fraction`；任一未知或未达标都保持验证档。外部汇总纪律率或回撤只能与逐笔日志交叉核对，不能覆盖日志。

### 7. 端到端计算器字段

需要精确股数时，把以下字段一次性传给 `evaluate_new_position_plan`：运行日 `decision_date`、计划日 09:25–09:29:59 且带 `+08:00` 的 `decision_time`、不晚于决策且在计划日 09:25 后的 `data_cutoff`、严格更早的 `latest_completed_trading_date`、严格布尔的 `trading_session_confirmed`、与运行日相同的 `rule_version_checked_at`、严格布尔 `rules_match_bundled_configuration` 与 `final_auction_data_confirmed`；最终竞价价 `final_auction_price` 和盘前冻结的 `forbidden_chase_price`；候选规范六位 `symbol`、`exchange` 及交叉核对用 `market_segment`；账户的 `equity/cash/open_order_exposure`；候选的 `entry_price/stop_price/target_price/sector_notional_cap/limit_down_fraction`；三轴与硬门的 `index_state/index_turning_up/sentiment_state/style_setup_match/mainline_state`（脚本自行生成环境阶段，不接受调用者直接指定 `environment_state`）；当日唯一 `setup` 与截至最近已完成交易日的完整 `trade_history`（脚本自行评估验证/升级档，不接受调用者直接指定 `risk_tier`）；组合的 `existing_portfolio_risk/existing_total_notional/existing_symbol_notional/existing_symbol_stress_fraction/existing_cluster_stress_fraction/existing_ice_trial_plan_count`；锁仓状态的 `daily_loss_fraction/stop_loss_count/weekly_loss_fraction/weekly_lock_remainder_of_week/weekly_recovery_days_remaining`；以及全部成本字段。脚本以 `max(entry_price, final_auction_price)` 形成 `effective_entry_price`，再从头计算结构止损距离、净 R、名义仓位、现金和风险上限。账户金额按 0.01 元精度输入，比例最多 12 位小数；不得额外传自定义申报网格、单笔风险率、组合风险率、环境总仓率、单股压力率或相关组压力率，这些硬值由脚本按代码、板块和档位固定。

## 输入校验顺序

1. 校验金额、价格、时间、比例和枚举值；价格须满足 `target > entry > stop > 0` 且落在 0.01 元价格网格。
2. 直接拒绝周末；校验精确端点的 `trading_session_confirmed=true`、`09:25<=decision_time<09:30`、`decision_date 09:25<=data_cutoff<=decision_time`、`latest_completed_trading_date<decision_date`、`bundled_rules_as_of<=decision_date`、`rule_version_checked_at=decision_date`、`exit_date<=latest_completed_trading_date`，证据和历史不得来自未来或盘前当日；交易会话、规则与最终竞价一致性声明必须为严格布尔真。
3. 从 `symbol+exchange` 派生板块，与 `market_segment` 交叉核对后固定申报网格和涨跌幅压力，并核对同证券压力与相关组压力。
4. 检查最终竞价是否越过禁止追价线或已经等于/跌破结构止损；后一种情况直接判定结构失效，不能用较高的盘前入场价掩盖。其余情况用不低于最终竞价价的有效入场价重算结构、净 R 和仓位，再检查未决委托、日锁、周锁和半风险剩余日数；冰点试错已有计划时拒绝第二只。
5. 按[决策框架](decision-framework.md)生成环境阶段与净 R 门槛。
6. 按[风险模型](risk-model.md)确定历史档、风险预算与合法申报股数。
7. 检查 T+1、隔夜、单股压力、相关压力、组合风险和总仓。

任一关键字段缺失时输出 `no_trade` 或 `conditional_watch`，同时写明缺失字段；不得继续给精确股数。

## 输出字段

输出至少包含以下结构：

```text
decision = {
  run_id,
  generated_at,
  decision_for,
  decision_time,
  latest_completed_trading_date,
  rule_version_checked_at,
  bundled_rules_as_of,
  rules_match_bundled_configuration,
  rule_attestation_independently_verified=false,
  trading_session_confirmed,
  trading_session_independently_verified=false,
  as_of,
  data_cutoff,
  final_auction_data_confirmed,
  final_auction_price,
  forbidden_chase_price,
  live_data_available,
  data_limitations,

  evidence_summary: {
    highest_grade,
    lowest_critical_grade,
    conflicts,
    missing_inputs
  },

  scope_checks: {
    eligible,
    exchange,
    security_type,
    exclusion_flags,
    exclusion_reason
  },

  environment: {
    trend_score,
    breadth_score,
    liquidity_score,
    short_sentiment_score,
    index_score,
    range_position_120d,
    index_state,
    index_turning_up,
    sentiment_state,
    style_state,
    style_setup_match,
    mainline_state,
    stage,
    stage_notional_cap_fraction,
    minimum_net_reward_risk
  },

  account_controls: {
    daily_lock,
    weekly_lock,
    lock_reason,
    weekly_recovery_days_remaining,
    recovery_multiplier,
    existing_ice_trial_plan_count
  },

  setup_evaluation: {
    setup,
    setup_trade_count,
    net_expectancy_r,
    discipline_execution_rate,
    setup_max_drawdown_fraction,
    upgrade_criteria_status,
    risk_tier,
    eligibility,
    invalidation
  },

  candidate_evaluations: [{
    symbol,
    name,
    setup,
    sector_state,
    sector_relative_strength,
    sector_breadth,
    sector_liquidity,
    sector_core_feedback,
    stock_role,
    net_reward_risk,
    entry_price,
    planned_entry_price,
    effective_entry_price,
    stop_price,
    target_price,
    final_auction_price,
    forbidden_chase_price,
    final_shares,
    action,
    reason_codes
  }],

  economics: {
    gross_reward_risk,
    net_reward_risk,
    net_reward_cash,
    net_risk_cash,
    costs_and_slippage,
    commission_basis,
    additional_fee_rate_per_side
  },

  sizing: {
    market_segment,
    limit_down_fraction,
    tier_risk_fraction,
    effective_risk_fraction,
    effective_risk_budget,
    stage_notional_cap,
    board_notional_cap,
    overnight_notional_cap,
    single_stock_stress_cap,
    correlated_stress_remaining,
    portfolio_risk_remaining,
    cash_cap,
    binding_limit,
    minimum_order_shares,
    share_increment,
    final_shares,
    final_notional,
    planned_risk_fraction,
    stress_fraction
  },

  action,
  reason_codes,
  conditions_before_entry,
  holding_plan: {
    flat_open_tolerance,
    opening_scenario,
    sellable_shares,
    locked_shares,
    exit_triggered,
    exit_filled,
    unfilled_reason,
    loss_add_forbidden,
    profit_add_requires_recalculation,
    expected_holding_days,
    validation_deadline
  },
  t_plus_one_plan,
  no_profit_guarantee
}
```

### `action` 枚举

| 值 | 含义 |
|---|---|
| `no_trade` | 存在硬否决、关键证据缺失、股数为 0 或风险超限。 |
| `conditional_watch` | 只差未来可观察的竞价/开盘确认；不输出预设成交股数。 |
| `allow_plan` | 所有规则通过，可给计划股数；仍不表示已经下单或必然成交。 |

脚本的端到端结果使用 `decision=trade/no_trade`。只有 `decision=trade` 且脚本外的范围、证据、主线、风格和成交可行性硬门也全部通过时，人类可读输出才映射为 `action=allow_plan`；脚本 `no_trade` 必须原样保持不交易。

### 推荐原因代码

- `DAILY_NEW_POSITION_LOCK`
- `WEEKLY_NEW_POSITION_LOCK`
- `OPEN_ORDER_EXPOSURE_UNRESOLVED`
- `FINAL_AUCTION_ABOVE_CHASE_LIMIT`
- `FINAL_AUCTION_INVALIDATES_PRICE_STRUCTURE`
- `ICE_TRIAL_PLAN_SLOT_OCCUPIED`
- `ENVIRONMENT_DEFENSE`
- `ENVIRONMENT_INSUFFICIENT`
- `MISSING_INPUT`
- `EVIDENCE_TOO_WEAK`
- `AXES_INCONSISTENT`
- `SETUP_MISMATCH`
- `STYLE_SETUP_MISMATCH`
- `MAINLINE_COLLAPSED`
- `ICEPOINT_UNREPAIRED`
- `OUT_OF_SCOPE`
- `UPGRADE_CRITERIA_UNMET`
- `NET_R_BELOW_THRESHOLD`
- `STOP_TOO_WIDE`
- `ONE_LOT_EXCEEDS_CAPS`
- `STAGE_CAP`
- `BOARD_CAP`
- `OVERNIGHT_CAP`
- `PORTFOLIO_RISK_CAP`
- `SINGLE_STOCK_STRESS_CAP`
- `CORRELATED_STRESS_CAP`
- `T1_OVERNIGHT_RISK`
- `VALIDATION_TIER`
- `UPGRADE_TIER`
- `HALF_RISK_WINDOW`
- `CROWDING_NEGATIVE`
- `LOSS_ADD_FORBIDDEN`
- `GAP_THROUGH_INVALIDATION`
- `EXIT_TRIGGERED_UNFILLED`
- `TIME_VALIDATION_EXPIRED`
- `SECTOR_FALLING`
- `SECTOR_UNKNOWN`
- `STOCK_ROLE_UNKNOWN`
- `FOLLOWER_DEPRIORITIZED`

## 人类可读输出顺序

面向用户的结果按以下顺序写，先结论后解释：

1. **结论**：是否交易、哪个模型、股数/仓位；若不交易直接写 0。
2. **环境**：三轴状态、阶段、环境上限和最低净 R。
3. **账户风控**：日/周锁、风险档、半风险乘数。
4. **经济性**：净 R、结构止损、费用与滑点假设。
5. **仓位瓶颈**：指出真正生效的最小上限。
6. **T+1 计划**：当日失效如何记录、最早何时可退出、跳空/跌停风险。
7. **证据与缺口**：数据截至时间、A/B/C 等级及未获得的实时信息。

在人类可读输出中，必须另列逐股候选表，至少包含：证券、唯一 `setup`、板块趋势、个股层级、净 R、入场价、止损价、目标价、合法申报股数、结论和原因代码。候选排序固定为合格核心、前排、跟风；`sector_state=unknown` 的行只能写条件观察，不能显示预设买入股数。

不得使用“必涨”“稳”“保证盈利”“最多亏损”等收益或损失保证表述。

## 决策日志字段

每次运行必须追加一条不可覆盖的日志，至少记录：

- 身份：`run_id`、`generated_at`、`decision_for`、`as_of`、`timezone`；
- 数据：`decision_time`、`data_cutoff`、`live_data_available`、交易会话核验与证据、最终竞价确认状态、最终竞价价、禁止追价线、原始输入摘要、所有 `missing_inputs`；
- 证据：每个事实的 `source`、`source_url_or_id`、`observed_at`、`evidence_grade`、冲突处理；
- 账户：权益、现金、日损失、周高点回撤、止损次数、周锁状态、半风险剩余日；
- 环境：四个指数分项、加权总分、120 日区间原始值与位置、`index_state`、`index_turning_up`、情绪、主导风格、风格适配、主线硬门、匹配到的矩阵行、阶段、环境总仓和净 R 门槛；
- 模型与候选：唯一 `setup`、同模型样本数、净期望、纪律执行率、该 setup 最大回撤、四项升级条件状态、验证/升级档、每只股票的板块趋势/相对强弱/广度/量能/核心反馈、个股层级、排序及降档原因；
- 价格：盘前入场价、最终竞价价、有效入场价、禁止追价线、止损、目标、止损距离、压力跌幅、数据来源；
- 成本：佣金率、最低佣金、佣金口径（全佣/净佣）、未包含的双边附加费、卖出税费、双边滑点，以及防重复计费检查；
- 计算：毛 R、净 R、单笔风险预算、所有股数上限、最低申报与递增单位取整过程、最终瓶颈；
- 组合：已有计划风险、候选新增风险、单股压力、各相关组压力、总仓、已有冰点试错计划数；
- 决策：范围排除检查、`action`、全部 `reason_codes`、条件、盘前平开容差、持仓四情景、可卖/锁定数量、1–3 日预期窗口、模型验证期限、失效点和 T+1 退出预案；
- 人工变化：用户是否要求超仓、追价或覆盖规则，以及系统为何拒绝；
- 事后回填：实际成交、退出、总费用、净盈亏、`net_r`、是否遵守计划、偏差原因。

日志不得保存券商密码、验证码、API 密钥、完整身份证号或可直接下单的凭据。

证据分级、官方链接和时效处理见[证据与交易规则](evidence-and-rules.md)。
