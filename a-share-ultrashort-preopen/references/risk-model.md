# 风险模型与确定性计算

规则时点：`as_of=2026-09-01`。

风险计算只描述计划损失和仓位上限，不保证止损价可成交。普通 A 股的 T+1、跳空和跌停可能让实际亏损超过计划值，具体交易制度见[证据与交易规则](evidence-and-rules.md)。

## 0. 脚本使用边界

脚本 `scripts/calc_trade_plan.py` 离线读取一个 JSON 请求并输出一个 JSON 响应，不联网、不接券商、不下单。精确新仓股数只能调用 CLI 操作 `evaluate_new_position_plan`；该操作只接受计划日 09:25–09:29:59 的北京时间决策、当日 09:25 后且不晚于决策时点的数据，并要求严格确认最终竞价数据。它从三轴输入自行判定环境、从当日 `setup` 的逐笔普通 A 股日志自行判定风险档，并由规范六位 `symbol` 与 `exchange` 派生板块，再把调用者的 `market_segment` 仅作为一致性复核，进而固定申报网格和普通股票压力跌幅。随后原子检查竞价追价线、防守禁令、冰点试错唯一计划、日周锁、未决委托、半风险期、组合剩余风险、环境/板块/相关压力上限、费用、净 R 和合法申报数量。它不接受调用者直接指定 `environment_state`、`risk_tier` 或自定义申报网格。低层仓位函数只供本地单元测试审计，不对 CLI 暴露，禁止把低层中间值当成最终建议。

推荐把请求保存为临时 JSON 文件后运行 `python3 scripts/calc_trade_plan.py request.json`。请求只允许以下两种格式二选一，禁止混用：

```text
{"operation": "...", "arguments": { ... }}
{"operation": "...", "field": "...", "other_field": "..."}
```

任一层级出现重复 JSON 键都会直接返回 `ok=false`；不得依赖 JSON 的“后键覆盖前键”改变锁仓、费用或仓位字段。精确端点直接拒绝周末，并要求严格布尔 `trading_session_confirmed=true`；该声明必须来自官方交易日历与临时休市核验，输出固定标记 `trading_session_independently_verified=false`。端点还要求 `bundled_rules_as_of<=decision_date`、`rule_version_checked_at=decision_date` 且严格布尔 `rules_match_bundled_configuration=true`。这些字段是调用方在读取官方来源后的可审计声明，离线脚本不会独立联网验证；脚本拒绝用当前规则快照回算更早日期。若未来计划日的最新规则与输出中的 `bundled_rules_as_of` 配置不一致，必须先更新脚本、文档和测试，不能仅把声明填成 `true`。

精确新仓使用 `evaluate_new_position_plan`；其字段见[输入输出契约](input-output.md)。其他公开操作仅用于分步审计：`classify_auction_phase`、`classify_environment_state`、`calculate_net_reward_risk`、`calculate_break_even_win_rate`、`get_environment_policy`、`calculate_overnight_notional_cap`、`get_risk_tier_config`、`evaluate_setup_tier`、`daily_lock_triggered`、`weekly_loss_action`。公开的 `calculate_net_reward_risk` 同样执行当前普通 A 股的最低真实摩擦校验，不能用零费率结果参与准入或排序。返回 `ok=false`、字段缺失或数值超出支持范围时不得自行补值。

## 1. 历史验证档

两个模型分别建账：`low_first_board` 与 `mainline_pullback`。任何样本、胜率或净期望不得跨模型合并。

| 档位 | 升降条件 | 单笔风险率 | 组合计划风险率 | 主板单股上限 | 20% 涨跌幅板块单股上限 | 账户总仓上限 | 单股跌停压力上限 |
|---|---|---:|---:|---:|---:|---:|---:|
| 验证档 `validation` | 默认；或四项升级条件中任一未知或不达标 | 0.75% | 1.5% | 30% | 15% | 60% | 3% |
| 升级档 `upgrade` | 同一 `setup` 至少 50 笔、扣费后净期望 `>0`、纪律执行率 `>=90%`、该 `setup` 最大回撤 `<=8%`；四项均已知且达标 | 1.5% | 2% | 30% | 20% | 60% | 4% |

确定性约束：

- 不存在 1.0% 中间档，也不能因“本月最好机会”“公开冠军会做”或“做完就停手”创建临时档。
- `net_expectancy_r = 同一 setup 有效样本 net_r 之和 / 有效样本数`。
- `discipline_execution_rate = 同一 setup 中 followed_plan=true 的完整记录数 / 同一 setup 完整记录总数`。
- `setup_max_drawdown_fraction` 必须来自按平仓时间排序、扣除全部成本后的该 setup 独立权益曲线：`max((历史峰值-随后权益)/历史峰值)`。
- 样本必须有唯一 `trade_id`、规范六位 `symbol`、匹配代码的 `exchange`、严格为真的 `trading_session_confirmed`、非空 `trading_session_evidence_id`、带 `+08:00` 的 `exit_timestamp`，并按该时间戳严格递增。脚本会拒绝 ETF、可转债等不在当前普通 A 股范围内的记录、周末记录以及 09:25 开盘撮合、09:30–11:30、13:00–15:00 之外的时间；节假日或临时休市由证据 ID 对应的外部官方日历核验。只填日期或同日任意重排不能用于计算回撤。记录还必须 `closed=true`、`costs_included=true`、模型标签固定并可追溯；权益曲线逐笔记录 `setup_equity_before/setup_equity_after`。`evaluate_setup_tier` 必须接收 `history_cutoff_date`，任何 `exit_date` 晚于截止日的记录都拒绝；盘前端点另要求 `latest_completed_trading_date < decision_date` 并以其作为截止日，因此当日伪造的“已平仓”记录不能提档。违反计划的交易必须保留在行为日志中，不能删掉亏损记录来制造正期望。
- 每行还要核对计划/实际成交、股数、毛盈亏、佣金、税费、附加费、滑点归因、净盈亏、计划风险和净 R。历史行必须保存当时的 `commission_rate/minimum_commission/commission_basis/additional_fee_rate_per_side/sell_tax_rate`；脚本按实际买入、卖出成交额分别重算费用，而不是只接受固定 10 元往返佣金，且普通 A 股佣金率不得超过官方上限 `0.003`。另以 `planned_entry_price/planned_stop_price/planned_slippage_rate_per_side`、股数和费用重算 `planned_risk_cash`；历史压力滑点必须在 `0.0005` 至 `0.05` 范围。为防止只给亏损行填极大滑点或费率来稀释亏损 R，统计盈利 R 使用实际配置与法定最低摩擦两次重算中较大的风险分母，统计亏损 R 使用两次重算中较小的风险分母。现金舍入容差只用于对账，不得叠加后穿越零点；成本重算、记录净盈亏与独立权益变化的盈亏符号必须一致。`slippage_cash` 已经反映在实际成交价形成的毛盈亏中，只做归因核对，不得从净盈亏再次扣除。
- 四项升级字段中任一未知或未达标，一律保持验证档。不得用主观评价补全纪律执行率或最大回撤。
- 升级后若同一统计窗口的净期望不再大于 0、纪律执行率低于 90% 或最大回撤超过 8%，立即回到验证档。

## 2. 日锁与周锁

损失比例均用正数表达，例如亏损 1.5% 输入 `0.015`。阈值包含等号。

### 日锁

`daily_loss_fraction = max(0, (day_start_equity - equity) / day_start_equity)`；`day_start_equity` 必须已按当日净外部现金流调整。

满足任一条件，立即锁定当日余下时间的新开仓：

- 当日损失率 `>=1.5%`；
- 当日出现第 2 次止损，即 `stop_loss_count>=2`。

日锁只允许按原计划管理已有持仓；禁止补仓、换股追回、扩大止损或以新账户规避。次一交易日可重新判断，但周锁仍有更高优先级。

### 周锁与后五日半风险

`weekly_loss_fraction = max(0, (week_high_equity - equity) / week_high_equity)`；`week_high_equity` 必须使用净外部现金流调整后的同口径权益。

当周内从本周权益高点的回撤 `>=5%`：

1. 锁定本自然交易周余下所有新开仓；
2. 本周结束后的下 5 个交易日允许交易，但新交易风险预算乘数固定为 `0.5`；
3. 第 6 个交易日起，若没有再次触发周锁，恢复该模型原档位。

例：周四触发 5% 周锁，周四触发后及周五禁止新开仓；下周一至周五是五个半风险交易日。不得改写成“五日完全禁开仓”。

半风险乘数作用于新交易的单笔风险预算和组合新增风险预算：

| 档位 | 常态单笔 | 后五日单笔 | 常态组合 | 后五日组合 |
|---|---:|---:|---:|---:|
| 验证档 | 0.75% | 0.375% | 1.5% | 0.75% |
| 升级档 | 1.5% | 0.75% | 2% | 1% |

环境、单股、账户总仓和压力上限仍是硬上限，半风险期不能放宽任何上限。若半风险期再次触发周锁，则先锁新一周余下时间，再从该周结束后重新计算 5 个半风险交易日。

该状态必须跨交易日持久化：`weekly_lock_remainder_of_week` 表示本周余下时间仍锁仓，`reduced_risk_trading_days_remaining` 表示周锁结束后剩余半风险交易日。触发或仍处于周锁时，剩余半风险日必须保持为 `5`；跨到下一自然周时只能解除前一周的锁定布尔值，不能把剩余日清零；每完成一个实际交易日后才递减 1。脚本不会猜测日历或替用户自动消耗剩余日。

## 3. 环境门槛

阶段由[决策框架](decision-framework.md)给出：

| 阶段 | 环境新开仓总仓上限 | 最低净 R |
|---|---:|---:|
| 进攻 | 60% | 2.0 |
| 谨慎 | 30% | 2.5 |
| 防守 | 0% | 不适用 |
| 冰点试错 | 10% | 3.0 |

环境上限与风险档总仓上限取较小者。达到最低净 R 只表示进入下一道风险检查，不代表应当交易。

## 4. 含成本净 R

输入必须使用同一币种和同一复权口径：

- `entry_price`：计划买入价；
- `stop_price`：结构失效价；
- `target_price`：有依据的目标价；
- `shares`：符合该证券最低申报数量和递增单位的股数；
- `commission_rate`、`minimum_commission`：用户券商实际参数；
- `commission_basis`：`all_in`（全佣）或 `net`（净佣）；
- `additional_fee_rate_per_side`：过户费、经手费、证管费等未包含在券商佣金中的双边附加费；
- `sell_tax_rate`：适用卖出税费参数；精确计划入口按 `as_of=2026-09-01` 拒绝低于 `0.0005` 的普通 A 股输入；
- `slippage_rate_per_side`：每边压力滑点，精确计划必须为正数；
- `decision_date`：计划交易日；
- `latest_completed_trading_date`：最近一个已完成交易日，必须早于 `decision_date`，并作为个人历史截止日。

上述费用字段均必须显式传入，不能依赖零费用默认值。精确计划按当前普通 A 股保守口径要求 `minimum_commission>=5` 元/边、`commission_rate<=0.003`、卖出印花税不低于 `0.0005`、每边压力滑点不低于 `0.0005`，且 `commission_rate + additional_fee_rate_per_side >= 0.0000641`。最后一个下限由双边证管费 `0.00002`、经手费 `0.0000341` 与过户费 `0.00001` 合计而来。这些是精确规划边界，不是对个股必然成交摩擦的断言；若实际费用与当前普通 A 股边界冲突，不输出精确股数并先核验口径。若 `commission_basis=all_in` 且全佣已经包含上述附加费，`additional_fee_rate_per_side` 必须填 `0`；若 `commission_basis=net`，`additional_fee_rate_per_side` 必须填写尚未包含的正费率。禁止把同一费用同时计入佣金和附加费。若官方收费规则变化，先更新规则时点、代码常量和测试，不得靠低报输入绕过。

多头计划必须满足 `target_price > entry_price > stop_price > 0`，且端到端入口的三个委托价格都必须落在普通 A 股 `0.01` 元价格最小变动单位上；账户金额只接受 0.01 元货币精度，比例最多保留 12 位小数，任意数值还受有效位和数量级限制。超精度数字直接拒绝，不参与门槛或硬上限比较。

计算：

```text
buy_fill = entry_price * (1 + slippage_rate_per_side)
stop_fill = stop_price * (1 - slippage_rate_per_side)
target_fill = target_price * (1 - slippage_rate_per_side)

buy_notional = buy_fill * shares
buy_commission = max(minimum_commission, buy_notional * commission_rate)
buy_additional_fees = buy_notional * additional_fee_rate_per_side
cash_out = buy_notional + buy_commission + buy_additional_fees

stop_notional = stop_fill * shares
stop_costs = max(minimum_commission, stop_notional * commission_rate)
             + stop_notional * additional_fee_rate_per_side
             + stop_notional * sell_tax_rate
net_risk_cash = cash_out - (stop_notional - stop_costs)

target_notional = target_fill * shares
target_costs = max(minimum_commission, target_notional * commission_rate)
               + target_notional * additional_fee_rate_per_side
               + target_notional * sell_tax_rate
net_reward_cash = (target_notional - target_costs) - cash_out

net_reward_risk = net_reward_cash / net_risk_cash
```

若 `net_risk_cash<=0`、`net_reward_cash<=0` 或参数缺失，则不得给出合格净 R。毛 R 达标而净 R 不达标时一律按不达标处理。

## 5. 结构止损与风险预算

```text
effective_entry_price = max(entry_price, final_auction_price)
stop_fraction = (effective_entry_price - stop_price) / effective_entry_price
base_risk_budget = equity * tier_risk_fraction
effective_risk_budget = base_risk_budget * recovery_multiplier
```

其中 `recovery_multiplier` 常态为 `1.0`，后五日半风险为 `0.5`。结构止损必须满足 `0 < stop_fraction <=5%`；超过 5% 直接拒绝，不通过缩小仓位把一个不合格结构伪装成合格交易。

计划股数先按价格风险计算：

```text
risk_limited_shares = floor(effective_risk_budget
                            / (effective_entry_price - stop_price))
```

这里的 `entry_price` 是 09:25 前冻结的最坏允许入场价。最终竞价价更高但尚未越过禁止追价线时，也必须把有效入场价上移后重新计算；不得继续使用旧价制造合格净 R。最终竞价价若已经等于或跌破 `stop_price`，结构立即失效，不能因 `max()` 仍取到较高盘前入场价而放行。若上移后目标不再高于有效入场价、止损不再有效或结构止损超过 5%，也直接不交易。

费用与滑点会让实际计划风险增大；最终输出需用合法申报股数重新计算含成本的 `net_risk_cash`，如超过有效风险预算，再按该证券递增单位下调直至不超限。

## 6. 名义仓位与合法申报数量

对每一候选交易，分别计算以下股数上限：

```text
effective_entry_price = max(entry_price, final_auction_price)
worst_entry_price = effective_entry_price * (1 + slippage_rate_per_side)
stage_shares = floor(stage_notional_cap / worst_entry_price)
board_and_sector_shares = floor(min(board_notional_cap, sector_notional_cap)
                                / worst_entry_price)
overnight_shares = floor(overnight_notional_cap / worst_entry_price)
cash_shares = floor(cash / worst_entry_price)
stress_shares = floor(single_stock_stress_cash_cap
                      / (worst_entry_price * limit_down_fraction))
```

其中：

- `stage_notional_cap = equity * 环境总仓上限 - existing_total_notional`；
- `board_notional_cap = equity * 对应风险档单股上限 - existing_symbol_notional`；同证券盈利加仓仍按新总额复核；
- `sector_notional_cap` 是当日对该题材或相关风险簇剩余的名义仓位上限；
- `overnight_notional_cap` 由固定单股压力上限和相关组剩余压力计算，不接受调用者任意放大；
- `single_stock_stress_cash_cap = equity * 单股跌停压力上限`；
- 在当前明确排除 ST、退市整理和新股无涨跌幅期的范围内，端到端脚本先由 `symbol+exchange` 派生板块，再与 `market_segment` 交叉核验，并固定使用：`main_board=10%`，`chinext/star=20%`。代码、交易所或板块标签不一致时直接拒绝；规则时点变化必须更新代码与测试。

取所有上限的最小值，再按证券当日有效的最低申报数量和递增单位向下取整：

```text
raw_shares = min(
    risk_limited_shares,
    stage_shares,
    board_and_sector_shares,
    overnight_shares,
    cash_shares,
    stress_shares,
)
if raw_shares < minimum_order_shares:
    final_shares = 0
else:
    final_shares = minimum_order_shares
                   + floor((raw_shares - minimum_order_shares)
                           / share_increment) * share_increment
```

端到端脚本不接受调用者覆盖 `minimum_order_shares` 和 `share_increment`，而是按代码派生且通过标签复核后的板块确定：主板与创业板为最低 100 股且按 100 股递增，科创板为最低 200 股、超过 200 股后按 1 股递增。若不足最低申报数量，结果为 0 股并输出 `ONE_LOT_EXCEEDS_CAPS`。禁止向上取整。只要 `open_order_exposure>0`，脚本先输出 `OPEN_ORDER_EXPOSURE_UNRESOLVED`，要求确认成交/撤单并刷新账户，不能靠现金相减继续给另一笔精确股数。取整后还必须用实际买入滑点、佣金和附加费复核现金与所有名义/风险上限；若超出，再按递增单位向下减少。

隔夜名义上限固定计算为：

```text
single_stress_remaining = max(0, 档位单股压力上限
                                  - existing_symbol_stress_fraction)
cluster_stress_remaining = max(0, 5% - existing_cluster_stress_fraction)
allowed_stress_fraction = min(single_stress_remaining,
                              cluster_stress_remaining)
overnight_notional_cap = equity * allowed_stress_fraction
                         / limit_down_fraction
```

`existing_symbol_stress_fraction` 必须等于 `existing_symbol_notional * limit_down_fraction / equity`（仅允许极小数值容差），并且不得大于所属 `existing_cluster_stress_fraction`。`limit_down_fraction` 是一次跌停压力假设，不是最大亏损保证。

## 7. 组合与相关性压力

所有已有持仓和候选仓位都要计入：

```text
position_planned_risk = 各持仓从参考价到结构止损的含成本计划损失
portfolio_planned_risk_fraction = sum(position_planned_risk) / equity

position_stress_fraction = position_notional * limit_down_fraction / equity
correlated_stress_fraction = 同一 correlation_group 的 position_stress_fraction 之和
```

必须同时满足：

- 组合计划风险不超过风险档上限；半风险期对新增风险使用减半后的上限；
- 任一单股压力不超过验证档 3% 或升级档 4%；
- 任一相关组压力合计 `<=5%`；
- 全账户名义总仓不超过风险档 60% 和环境上限的较小者。

两只同题材股票不能被当作分散化。若同一主线核心失效会同时伤害两只股票，就必须使用同一个 `correlation_group`。

## 8. T+1 与实际亏损边界

普通 A 股当日买入后，结构止损可能在当日触发但无法当日卖出。计划必须同时记录：

- `intraday_invalidation_price`：当日观察到失效的价格，只用于记录；
- `earliest_exit_session`：规则允许的最早退出交易日；
- `gap_and_limit_risk`：次日跳空或无法成交导致超过 1R 的风险；
- `overnight_notional_cap`：由风险档单股压力与相关组剩余 5% 压力共同算出的名义金额上限。

因此“单笔风险 0.75%/1.5%”是按计划止损计算的预算，不是最大亏损保证。输出不得使用“最多只亏”“保证止损”之类表述。
