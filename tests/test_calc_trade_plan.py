import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date, time, timedelta
from decimal import Decimal, getcontext
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TARGET_SCRIPT = (
    WORKSPACE_ROOT
    / "a-share-ultrashort-preopen"
    / "scripts"
    / "calc_trade_plan.py"
)
TARGET_IMPLEMENTED = TARGET_SCRIPT.is_file()


def load_target_module():
    spec = importlib.util.spec_from_file_location("calc_trade_plan", TARGET_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load target module from {TARGET_SCRIPT}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TargetImplementationContractTests(unittest.TestCase):
    def test_target_implementation_exists(self):
        self.assertTrue(
            TARGET_SCRIPT.is_file(),
            f"RED: production implementation is missing: {TARGET_SCRIPT}",
        )

    def test_import_does_not_mutate_callers_decimal_context(self):
        original_precision = getcontext().prec
        try:
            getcontext().prec = 37
            load_target_module()
            self.assertEqual(getcontext().prec, 37)
        finally:
            getcontext().prec = original_precision


@unittest.skipUnless(
    TARGET_IMPLEMENTED,
    f"waiting for production implementation: {TARGET_SCRIPT}",
)
class CalcTradePlanTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calc = load_target_module()


class AuctionPhaseTests(CalcTradePlanTestCase):
    def test_before_0915_is_before_call_auction(self):
        self.assertEqual(
            self.calc.classify_auction_phase(time(9, 14, 59)),
            "before_call_auction",
        )

    def test_0915_through_0919_is_cancelable_call_auction(self):
        for current_time in (time(9, 15), time(9, 19, 59)):
            with self.subTest(current_time=current_time):
                self.assertEqual(
                    self.calc.classify_auction_phase(current_time),
                    "cancelable_call_auction",
                )

    def test_0920_through_0924_is_non_cancelable_call_auction(self):
        for current_time in (time(9, 20), time(9, 24, 59)):
            with self.subTest(current_time=current_time):
                self.assertEqual(
                    self.calc.classify_auction_phase(current_time),
                    "non_cancelable_call_auction",
                )

    def test_0925_and_later_is_post_call_auction(self):
        for current_time in (time(9, 25), time(9, 25, 1)):
            with self.subTest(current_time=current_time):
                self.assertEqual(
                    self.calc.classify_auction_phase(current_time),
                    "post_call_auction",
                )


class NetRewardRiskTests(CalcTradePlanTestCase):
    def test_costs_reduce_gross_2_08r_below_net_2r(self):
        entry_price = 10.0
        stop_price = 9.9
        target_price = 10.208

        gross_rr = (target_price - entry_price) / (entry_price - stop_price)
        self.assertAlmostEqual(gross_rr, 2.08)

        net_rr = self.calc.calculate_net_reward_risk(
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            shares=1_000,
            commission_rate=0.0003,
            minimum_commission=5.0,
            sell_tax_rate=0.0005,
            slippage_rate_per_side=0.001,
            additional_fee_rate_per_side=0.0,
            commission_basis="all_in",
        )

        # Buy and sell both suffer 0.1% slippage.  Commission is the RMB 5
        # minimum on each side, and tax is charged only on sale proceeds.
        self.assertAlmostEqual(net_rr, 1.280678111654853, places=9)
        self.assertLess(net_rr, 2.0)

    def test_break_even_win_rate_uses_net_reward_risk(self):
        self.assertAlmostEqual(
            self.calc.calculate_break_even_win_rate(net_reward_risk=2.5),
            1 / 3.5,
        )

    def test_additional_per_side_fees_reduce_net_reward_risk(self):
        base = {
            "entry_price": 10.0,
            "stop_price": 9.8,
            "target_price": 10.6,
            "shares": 1_000,
            "commission_rate": 0.0003,
            "minimum_commission": 5.0,
            "sell_tax_rate": 0.0005,
            "slippage_rate_per_side": 0.001,
            "additional_fee_rate_per_side": 0.0,
            "commission_basis": "all_in",
        }

        without_additional_fees = self.calc.calculate_net_reward_risk(**base)
        with_additional_fee_inputs = dict(base)
        with_additional_fee_inputs.update(
            additional_fee_rate_per_side=0.0001,
            commission_basis="net",
        )
        with_additional_fees = self.calc.calculate_net_reward_risk(
            **with_additional_fee_inputs
        )

        self.assertLess(with_additional_fees, without_additional_fees)

    def test_public_net_reward_risk_rejects_zero_friction_inputs(self):
        with self.assertRaisesRegex(
            ValueError,
            r"commission|tax|slippage|cost",
        ):
            self.calc.calculate_net_reward_risk(
                entry_price=10.0,
                stop_price=9.5,
                target_price=11.0,
                shares=1_000,
                commission_rate=0,
                minimum_commission=0,
                sell_tax_rate=0,
                slippage_rate_per_side=0,
                additional_fee_rate_per_side=0,
                commission_basis="all_in",
            )


class PositionSizingTests(CalcTradePlanTestCase):
    def base_position_inputs(self):
        return {
            "cash": 100_000.0,
            "entry_price": 10.0,
            "stop_price": 9.5,
            "risk_budget": 100_000.0,
            "stage_notional_cap": 100_000.0,
            "board_notional_cap": 100_000.0,
            "overnight_notional_cap": 100_000.0,
            "minimum_order_shares": 100,
            "share_increment": 100,
            "commission_rate": 0.0,
            "minimum_commission": 0.0,
            "sell_tax_rate": 0.0,
            "slippage_rate_per_side": 0.0,
            "additional_fee_rate_per_side": 0.0,
            "commission_basis": "all_in",
        }

    def test_each_cap_can_be_the_binding_position_limit(self):
        cases = (
            ("risk", {"risk_budget": 275.0}, 500),
            ("stage", {"stage_notional_cap": 4_700.0}, 400),
            ("board", {"board_notional_cap": 3_900.0}, 300),
            ("overnight", {"overnight_notional_cap": 2_800.0}, 200),
            ("cash", {"cash": 1_900.0}, 100),
        )

        for limiter, overrides, expected_shares in cases:
            inputs = self.base_position_inputs()
            inputs.update(overrides)
            with self.subTest(limiter=limiter):
                self.assertEqual(
                    self.calc.calculate_position_shares(**inputs),
                    expected_shares,
                )

    def test_smallest_cap_is_selected_before_100_share_rounding(self):
        inputs = self.base_position_inputs()
        inputs.update(
            risk_budget=450.0,  # 900 shares by risk
            stage_notional_cap=8_000.0,  # 800 shares
            board_notional_cap=7_000.0,  # 700 shares
            overnight_notional_cap=6_500.0,  # 650 shares: binding
            cash=10_000.0,  # 1,000 shares
        )

        self.assertEqual(self.calc.calculate_position_shares(**inputs), 600)

    def test_star_market_can_use_200_share_minimum_then_one_share_increments(self):
        inputs = self.base_position_inputs()
        inputs.update(
            overnight_notional_cap=5_900.0,
            minimum_order_shares=200,
            share_increment=1,
        )

        self.assertEqual(self.calc.calculate_position_shares(**inputs), 590)

    def test_structural_stop_wider_than_five_percent_is_rejected(self):
        inputs = self.base_position_inputs()
        inputs.update(stop_price=9.49)

        with self.assertRaisesRegex(ValueError, r"(?i)structural stop.*5%"):
            self.calc.calculate_position_shares(**inputs)

    def test_returns_zero_when_one_lot_exceeds_risk_budget(self):
        inputs = self.base_position_inputs()
        inputs.update(risk_budget=49.0)

        self.assertEqual(self.calc.calculate_position_shares(**inputs), 0)

    def test_position_risk_includes_fees_tax_and_slippage(self):
        inputs = self.base_position_inputs()
        inputs.update(
            risk_budget=260.0,
            commission_rate=0.0003,
            minimum_commission=5.0,
            sell_tax_rate=0.0005,
            slippage_rate_per_side=0.001,
        )

        self.assertEqual(self.calc.calculate_position_shares(**inputs), 400)

    def test_position_cash_limit_includes_buy_costs(self):
        inputs = self.base_position_inputs()
        inputs.update(
            cash=1_000.0,
            commission_rate=0.0003,
            minimum_commission=5.0,
            sell_tax_rate=0.0005,
            slippage_rate_per_side=0.001,
        )

        self.assertEqual(self.calc.calculate_position_shares(**inputs), 0)

    def test_slippage_cannot_cross_a_notional_cap(self):
        inputs = self.base_position_inputs()
        inputs.update(
            entry_price=10.0,
            stage_notional_cap=1_000.0,
            slippage_rate_per_side=0.001,
        )

        self.assertEqual(self.calc.calculate_position_shares(**inputs), 0)

    def test_exact_five_percent_structural_stop_is_allowed(self):
        inputs = self.base_position_inputs()
        inputs.update(stop_price=9.5)

        self.assertGreater(self.calc.calculate_position_shares(**inputs), 0)

    def test_all_cost_fields_are_required_for_exact_share_output(self):
        inputs = self.base_position_inputs()
        inputs.pop("commission_rate")

        with self.assertRaises(TypeError):
            self.calc.calculate_position_shares(**inputs)


class EnvironmentPolicyTests(CalcTradePlanTestCase):
    def test_approved_environment_policies_are_fixed(self):
        expected = {
            "attack": {"total_cap": 0.60, "minimum_net_r": 2.0},
            "cautious": {"total_cap": 0.30, "minimum_net_r": 2.5},
            "defense": {"total_cap": 0.0, "minimum_net_r": None},
            "ice_trial": {"total_cap": 0.10, "minimum_net_r": 3.0},
        }

        for state, policy in expected.items():
            with self.subTest(state=state):
                self.assertEqual(self.calc.get_environment_policy(state), policy)

    def test_index_rising_cannot_override_sentiment_retreat(self):
        self.assertEqual(
            self.calc.classify_environment_state(
                index_state="rising",
                index_turning_up=False,
                sentiment_state="retreat",
                style_setup_match="matched",
                mainline_state="confirmed",
            ),
            "defense",
        )

    def test_falling_index_only_allows_the_exact_icepoint_repair_exception(self):
        common = {
            "index_state": "falling",
            "index_turning_up": False,
            "style_setup_match": "matched",
        }
        self.assertEqual(
            self.calc.classify_environment_state(
                **common,
                sentiment_state="repair",
                mainline_state="confirmed",
            ),
            "defense",
        )
        self.assertEqual(
            self.calc.classify_environment_state(
                **common,
                sentiment_state="icepoint_repair",
                mainline_state="low_resonance",
            ),
            "ice_trial",
        )

    def test_attack_requires_all_positive_axes_and_mainline(self):
        self.assertEqual(
            self.calc.classify_environment_state(
                index_state="rising",
                index_turning_up=False,
                sentiment_state="main_rise",
                style_setup_match="matched",
                mainline_state="confirmed",
            ),
            "attack",
        )

    def test_unknown_axis_is_insufficient_not_a_tradable_state(self):
        self.assertEqual(
            self.calc.classify_environment_state(
                index_state="unknown",
                index_turning_up=False,
                sentiment_state="main_rise",
                style_setup_match="matched",
                mainline_state="confirmed",
            ),
            "insufficient",
        )


class OvernightStressTests(CalcTradePlanTestCase):
    def test_main_board_validation_stress_caps_notional_at_30_percent(self):
        self.assertEqual(
            self.calc.calculate_overnight_notional_cap(
                equity=30_000,
                limit_down_fraction=0.10,
                tier="validation",
                existing_single_stress_fraction=0.0,
                existing_cluster_stress_fraction=0.0,
            ),
            9_000.0,
        )

    def test_existing_cluster_stress_reduces_remaining_notional(self):
        self.assertEqual(
            self.calc.calculate_overnight_notional_cap(
                equity=30_000,
                limit_down_fraction=0.10,
                tier="validation",
                existing_single_stress_fraction=0.0,
                existing_cluster_stress_fraction=0.03,
            ),
            6_000.0,
        )

    def test_existing_same_symbol_stress_reduces_single_name_room(self):
        self.assertEqual(
            self.calc.calculate_overnight_notional_cap(
                equity=30_000,
                limit_down_fraction=0.10,
                tier="validation",
                existing_single_stress_fraction=0.03,
                existing_cluster_stress_fraction=0.03,
            ),
            0.0,
        )


class RiskTierTests(CalcTradePlanTestCase):
    CUTOFF_DATE = "2026-09-01"

    def evaluate_setup(self, history, setup="low_first_board", **kwargs):
        return self.calc.evaluate_setup_tier(
            history,
            setup,
            history_cutoff_date=self.CUTOFF_DATE,
            **kwargs,
        )

    @staticmethod
    def make_history(setup, wins, losses, win_r=1.2):
        net_results = [win_r] * wins + [-1.0] * losses
        equity = Decimal("30000")
        history = []
        trade_dates = []
        cursor = date(2026, 1, 5)
        while len(trade_dates) < len(net_results):
            if cursor.weekday() < 5:
                trade_dates.append(cursor)
            cursor += timedelta(days=1)
        for index, (net_r, trade_date) in enumerate(zip(net_results, trade_dates)):
            equity_before = equity
            net_r_decimal = Decimal(str(net_r))
            commission_rate = Decimal("0.0003")
            minimum_commission = Decimal("5")
            sell_tax_rate = Decimal("0.0005")
            planned_slippage_rate = Decimal("0.0005")
            entry_price = Decimal("10")
            planned_stop_price = Decimal("9.90")
            shares = 100
            quantity = Decimal(shares)
            planned_buy_notional = (
                entry_price * (Decimal("1") + planned_slippage_rate) * quantity
            )
            planned_stop_notional = (
                planned_stop_price
                * (Decimal("1") - planned_slippage_rate)
                * quantity
            )
            planned_buy_cash = planned_buy_notional + max(
                minimum_commission,
                planned_buy_notional * commission_rate,
            )
            planned_stop_cash = (
                planned_stop_notional
                - max(
                    minimum_commission,
                    planned_stop_notional * commission_rate,
                )
                - planned_stop_notional * sell_tax_rate
            )
            planned_risk_cash = planned_buy_cash - planned_stop_cash
            net_pnl = net_r_decimal * planned_risk_cash
            commission_cash = Decimal("10")
            exit_price = (
                net_pnl + entry_price * quantity + commission_cash
            ) / (quantity * (Decimal("1") - sell_tax_rate))
            tax_cash = exit_price * quantity * sell_tax_rate
            gross_pnl_cash = (exit_price - entry_price) * quantity
            equity += net_pnl
            history.append(
                {
                    "trade_id": f"{setup}-{index}",
                    "exit_date": trade_date.isoformat(),
                    "exit_timestamp": trade_date.isoformat() + "T15:00:00+08:00",
                    "trading_session_confirmed": True,
                    "trading_session_evidence_id": f"calendar-{trade_date.isoformat()}",
                    "setup": setup,
                    "symbol": "600000",
                    "exchange": "SSE",
                    "planned_entry_price": str(entry_price),
                    "planned_stop_price": str(planned_stop_price),
                    "planned_exit_price": str(exit_price),
                    "entry_price": str(entry_price),
                    "exit_price": str(exit_price),
                    "shares": shares,
                    "gross_pnl_cash": str(gross_pnl_cash),
                    "commission_cash": str(commission_cash),
                    "tax_cash": str(tax_cash),
                    "additional_fees_cash": "0",
                    "commission_rate": str(commission_rate),
                    "minimum_commission": str(minimum_commission),
                    "commission_basis": "all_in",
                    "additional_fee_rate_per_side": "0",
                    "sell_tax_rate": str(sell_tax_rate),
                    "planned_slippage_rate_per_side": str(
                        planned_slippage_rate
                    ),
                    "slippage_cash": "0",
                    "net_pnl": str(net_pnl),
                    "planned_risk_cash": str(planned_risk_cash),
                    "net_r": str(net_r_decimal),
                    "followed_plan": True,
                    "closed": True,
                    "costs_included": True,
                    "setup_equity_before": str(equity_before),
                    "setup_equity_after": str(equity),
                    "exit_reason": "planned_exit",
                    "evidence_log_id": f"snapshot-{index}",
                }
            )
        return history

    def test_etf_history_cannot_unlock_an_a_share_risk_tier(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        for trade in history:
            trade["symbol"] = "510300"
            trade["exchange"] = "SSE"

        with self.assertRaisesRegex(ValueError, r"ordinary A-share|symbol"):
            self.evaluate_setup(history)

    def test_history_requires_exchange_for_security_scope_validation(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1].pop("exchange")

        with self.assertRaisesRegex(ValueError, r"exchange"):
            self.evaluate_setup(history)

    def test_weekend_history_cannot_count_toward_upgrade(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[0]["exit_date"] = "2026-01-03"
        history[0]["exit_timestamp"] = "2026-01-03T15:00:00+08:00"

        with self.assertRaisesRegex(ValueError, r"weekend|trading session"):
            self.evaluate_setup(history)

    def test_off_session_history_timestamp_cannot_count_toward_upgrade(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1]["exit_timestamp"] = (
            history[-1]["exit_date"] + "T23:59:00+08:00"
        )

        with self.assertRaisesRegex(ValueError, r"trading session|time"):
            self.evaluate_setup(history)

    def test_history_requires_strict_session_attestation_and_evidence(self):
        for mutation in ("missing_attestation", "false_attestation", "missing_evidence"):
            history = self.make_history("low_first_board", wins=30, losses=20)
            if mutation == "missing_attestation":
                history[-1].pop("trading_session_confirmed")
            elif mutation == "false_attestation":
                history[-1]["trading_session_confirmed"] = False
            else:
                history[-1].pop("trading_session_evidence_id")

            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(
                    (TypeError, ValueError),
                    r"trading_session|evidence",
                ):
                    self.evaluate_setup(history)

    @staticmethod
    def set_last_equity_after(history, equity_after):
        trade = history[-1]
        before = Decimal(str(trade["setup_equity_before"]))
        after = Decimal(str(equity_after))
        net_pnl = after - before
        planned_risk_cash = Decimal("1000")
        commission_rate = Decimal("0.0003")
        minimum_commission = Decimal("5")
        sell_tax_rate = Decimal("0.0005")
        planned_slippage_rate = Decimal("0.0005")
        shares = 5_000
        quantity = Decimal(shares)
        entry_price = Decimal("10")
        planned_stop_price = Decimal("9.50")
        planned_buy_notional = (
            entry_price * (Decimal("1") + planned_slippage_rate) * quantity
        )
        planned_stop_notional = (
            planned_stop_price
            * (Decimal("1") - planned_slippage_rate)
            * quantity
        )
        planned_buy_cash = planned_buy_notional + max(
            minimum_commission,
            planned_buy_notional * commission_rate,
        )
        planned_stop_cash = (
            planned_stop_notional
            - max(
                minimum_commission,
                planned_stop_notional * commission_rate,
            )
            - planned_stop_notional * sell_tax_rate
        )
        planned_risk_cash = planned_buy_cash - planned_stop_cash
        exit_price = (
            net_pnl
            + entry_price * quantity * (Decimal("1") + commission_rate)
        ) / (
            quantity
            * (Decimal("1") - commission_rate - sell_tax_rate)
        )
        commission_cash = (
            entry_price * quantity * commission_rate
            + exit_price * quantity * commission_rate
        )
        tax_cash = exit_price * quantity * sell_tax_rate
        gross_pnl_cash = (exit_price - entry_price) * quantity
        trade.update(
            planned_entry_price=str(entry_price),
            planned_stop_price=str(planned_stop_price),
            planned_exit_price=str(exit_price),
            entry_price=str(entry_price),
            exit_price=str(exit_price),
            shares=shares,
            gross_pnl_cash=str(gross_pnl_cash),
            commission_cash=str(commission_cash),
            tax_cash=str(tax_cash),
            commission_rate="0.0003",
            minimum_commission="5",
            commission_basis="all_in",
            additional_fee_rate_per_side="0",
            sell_tax_rate=str(sell_tax_rate),
            planned_slippage_rate_per_side=str(planned_slippage_rate),
            net_pnl=str(net_pnl),
            planned_risk_cash=str(planned_risk_cash),
            net_r=str(net_pnl / planned_risk_cash),
            setup_equity_after=str(after),
        )

    def test_positive_setup_with_fewer_than_50_trades_stays_validation_tier(self):
        history = self.make_history("low_first_board", wins=30, losses=19)

        decision = self.evaluate_setup(history)

        self.assertEqual(decision["tier"], "validation")
        self.assertEqual(decision["risk_fraction"], 0.0075)
        self.assertEqual(decision["setup_trade_count"], 49)

    def test_only_the_two_approved_setups_can_be_evaluated(self):
        with self.assertRaisesRegex(ValueError, r"approved setup"):
            self.evaluate_setup([], "board_relay")

    def test_50_positive_trades_in_one_setup_reach_upgrade_tier(self):
        history = self.make_history("low_first_board", wins=30, losses=20)

        decision = self.evaluate_setup(history)

        self.assertGreater(decision["net_expectancy_r"], 0.0)
        self.assertEqual(decision["setup_trade_count"], 50)
        self.assertEqual(decision["tier"], "upgrade")
        self.assertEqual(decision["risk_fraction"], 0.015)

    def test_unknown_max_drawdown_cannot_unlock_upgrade(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1].pop("setup_equity_after")

        decision = self.evaluate_setup(history)

        self.assertEqual(decision["tier"], "validation")
        self.assertIsNone(decision["max_drawdown_fraction"])

    def test_missing_discipline_records_cannot_unlock_upgrade(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1].pop("followed_plan")

        decision = self.evaluate_setup(history)

        self.assertEqual(decision["tier"], "validation")
        self.assertIsNone(decision["discipline_rate"])

    def test_50_trades_pooled_across_setups_do_not_unlock_upgrade(self):
        history = self.make_history("low_first_board", wins=11, losses=9)
        history += self.make_history(
            "mainline_pullback",
            wins=9,
            losses=21,
            win_r=3.0,
        )

        decision = self.evaluate_setup(history)

        self.assertEqual(len(history), 50)
        self.assertEqual(decision["setup_trade_count"], 20)
        self.assertEqual(decision["tier"], "validation")
        self.assertEqual(decision["risk_fraction"], 0.0075)

    def test_50_negative_expectancy_trades_stay_validation_tier(self):
        history = self.make_history("low_first_board", wins=20, losses=30)

        decision = self.evaluate_setup(history)

        self.assertLess(decision["net_expectancy_r"], 0.0)
        self.assertEqual(decision["tier"], "validation")
        self.assertEqual(decision["risk_fraction"], 0.0075)

    def test_upgrade_is_blocked_when_discipline_is_below_90_percent(self):
        history = self.make_history("low_first_board", wins=30, losses=20)

        for trade in history[-6:]:
            trade["followed_plan"] = False

        decision = self.evaluate_setup(history)

        self.assertEqual(decision["tier"], "validation")
        self.assertEqual(decision["risk_fraction"], 0.0075)

    def test_upgrade_is_blocked_when_drawdown_exceeds_eight_percent(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        peak = max(
            Decimal(str(trade[field]))
            for trade in history
            for field in ("setup_equity_before", "setup_equity_after")
        )
        self.set_last_equity_after(history, peak * Decimal("0.9199"))

        decision = self.evaluate_setup(history)

        self.assertEqual(decision["tier"], "validation")
        self.assertEqual(decision["risk_fraction"], 0.0075)

    def test_upgrade_allows_exact_discipline_and_drawdown_boundaries(self):
        history = self.make_history("low_first_board", wins=30, losses=20)

        for trade in history[-5:]:
            trade["followed_plan"] = False
        peak = max(
            Decimal(str(trade[field]))
            for trade in history
            for field in ("setup_equity_before", "setup_equity_after")
        )
        self.set_last_equity_after(history, peak * Decimal("0.92"))

        decision = self.evaluate_setup(history)

        self.assertEqual(decision["tier"], "upgrade")
        self.assertEqual(decision["risk_fraction"], 0.015)

    def test_external_discipline_cannot_override_observed_history(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        for trade in history:
            trade["followed_plan"] = False

        with self.assertRaisesRegex(ValueError, r"discipline.*conflict"):
            self.evaluate_setup(history, discipline_rate=0.90)

    def test_unclosed_or_cost_incomplete_records_cannot_count(self):
        for field, value in (("closed", False), ("costs_included", False)):
            history = self.make_history("low_first_board", wins=30, losses=20)
            history[-1][field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    self.evaluate_setup(history)

    def test_missing_trade_cost_detail_cannot_unlock_upgrade(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1].pop("commission_cash")

        with self.assertRaisesRegex((TypeError, ValueError), r"commission_cash"):
            self.evaluate_setup(history)

    def test_understated_historical_sell_tax_cannot_unlock_upgrade(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        for trade in history:
            tax_cash = Decimal(str(trade["tax_cash"]))
            trade["gross_pnl_cash"] = str(
                Decimal(str(trade["gross_pnl_cash"])) - tax_cash
            )
            trade["exit_price"] = str(
                Decimal(str(trade["entry_price"]))
                + Decimal(str(trade["gross_pnl_cash"]))
                / Decimal(trade["shares"])
            )
            trade["planned_exit_price"] = trade["exit_price"]
            trade["tax_cash"] = "0"

        with self.assertRaisesRegex(ValueError, r"tax_cash.*(?:reconcile|rule floor)"):
            self.evaluate_setup(history)

    def test_understated_historical_commission_cannot_unlock_upgrade(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        for trade in history:
            commission_cash = Decimal(str(trade["commission_cash"]))
            trade["gross_pnl_cash"] = str(
                Decimal(str(trade["gross_pnl_cash"])) - commission_cash
            )
            trade["exit_price"] = str(
                Decimal(str(trade["entry_price"]))
                + Decimal(str(trade["gross_pnl_cash"]))
                / Decimal(trade["shares"])
            )
            trade["planned_exit_price"] = trade["exit_price"]
            trade["commission_cash"] = "0"

        with self.assertRaisesRegex(ValueError, r"commission_cash.*(?:reconcile|floor)"):
            self.evaluate_setup(history)

    def test_large_notional_history_must_reconcile_commission_to_its_rate(self):
        history = self.make_history("low_first_board", wins=50, losses=0)
        equity = Decimal("30000")
        for index, trade in enumerate(history):
            entry = Decimal("10")
            quantity = Decimal("100000")
            reported_commission = Decimal("10")
            sell_tax_rate = Decimal("0.0005")
            net_pnl = Decimal("1")
            exit_price = (
                net_pnl + entry * quantity + reported_commission
            ) / (quantity * (Decimal("1") - sell_tax_rate))
            tax_cash = exit_price * quantity * sell_tax_rate
            gross = (exit_price - entry) * quantity
            equity_before = equity
            equity += net_pnl
            planned_risk = Decimal(str(trade["planned_risk_cash"]))
            trade.update(
                trade_id=f"large-{index}",
                planned_entry_price=str(entry),
                planned_exit_price=str(exit_price),
                entry_price=str(entry),
                exit_price=str(exit_price),
                shares=int(quantity),
                gross_pnl_cash=str(gross),
                commission_cash=str(reported_commission),
                tax_cash=str(tax_cash),
                net_pnl=str(net_pnl),
                planned_risk_cash=str(planned_risk),
                net_r=str(net_pnl / planned_risk),
                setup_equity_before=str(equity_before),
                setup_equity_after=str(equity),
            )

        with self.assertRaisesRegex(ValueError, r"commission_cash.*reconcile"):
            self.evaluate_setup(history)

    def test_reported_planned_risk_cannot_manufacture_positive_expectancy(self):
        history = self.make_history(
            "low_first_board",
            wins=25,
            losses=25,
            win_r=0.5,
        )
        for trade in history:
            net_pnl = Decimal(str(trade["net_pnl"]))
            fake_risk = Decimal("1") if net_pnl > 0 else Decimal("1000")
            trade["planned_risk_cash"] = str(fake_risk)
            trade["net_r"] = str(net_pnl / fake_risk)

        with self.assertRaisesRegex(ValueError, r"planned_risk_cash.*reconcile"):
            self.evaluate_setup(history)

    def test_extreme_planned_slippage_cannot_dilute_historical_loss_r(self):
        history = self.make_history(
            "low_first_board",
            wins=25,
            losses=25,
            win_r=0.1,
        )
        extreme_slippage = Decimal("100")
        for trade in history:
            net_pnl = Decimal(str(trade["net_pnl"]))
            if net_pnl >= 0:
                continue
            entry = Decimal(str(trade["planned_entry_price"]))
            stop = Decimal(str(trade["planned_stop_price"]))
            quantity = Decimal(trade["shares"])
            commission_rate = Decimal(str(trade["commission_rate"]))
            minimum = Decimal(str(trade["minimum_commission"]))
            tax_rate = Decimal(str(trade["sell_tax_rate"]))
            buy_notional = entry * (Decimal("1") + extreme_slippage) * quantity
            stop_notional = stop * (Decimal("1") - extreme_slippage) * quantity
            buy_cash = buy_notional + max(
                minimum,
                buy_notional * commission_rate,
            )
            stop_cash = (
                stop_notional
                - max(minimum, stop_notional * commission_rate)
                - stop_notional * tax_rate
            )
            fake_risk = buy_cash - stop_cash
            trade["planned_slippage_rate_per_side"] = str(extreme_slippage)
            trade["planned_risk_cash"] = str(fake_risk)
            trade["net_r"] = str(
                (net_pnl / fake_risk).quantize(Decimal("1e-30"))
            )

        with self.assertRaisesRegex(ValueError, r"planned_slippage.*(?:less|5%)"):
            self.evaluate_setup(history)

    def test_impossible_historical_commission_rate_is_rejected_before_r_math(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[0]["commission_rate"] = "0.9"

        with self.assertRaisesRegex(ValueError, r"commission_rate.*(?:0.003|upper)"):
            self.evaluate_setup(history)

    def test_exit_timestamps_must_be_strictly_increasing(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        for index, trade in enumerate(history):
            trade["exit_date"] = "2026-08-31"
            trade["exit_timestamp"] = (
                f"2026-08-31T14:{49 - index:02d}:00+08:00"
            )

        with self.assertRaisesRegex(ValueError, r"exit_timestamp.*order"):
            self.evaluate_setup(history)

    def test_net_r_must_reconcile_to_net_pnl_and_planned_risk(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1]["net_r"] = "99"

        with self.assertRaisesRegex(ValueError, r"net_r.*reconcile"):
            self.evaluate_setup(history)

    def test_reconciliation_tolerance_cannot_turn_zero_ev_positive(self):
        history = self.make_history("low_first_board", wins=50, losses=0, win_r=0)
        for trade in history:
            trade["net_r"] = "0.0000009"

        decision = self.evaluate_setup(history)

        self.assertEqual(decision["net_expectancy_r"], 0.0)
        self.assertEqual(decision["tier"], "validation")

    def test_cash_tolerance_cannot_turn_a_declining_equity_curve_positive(self):
        history = self.make_history("low_first_board", wins=50, losses=0, win_r=0)
        equity = Decimal("30000")
        for trade in history:
            net_pnl = Decimal("0.009")
            commission = Decimal("10")
            tax_rate = Decimal("0.0005")
            entry = Decimal("10")
            quantity = Decimal("100")
            exit_price = (
                net_pnl + entry * quantity + commission
            ) / (quantity * (Decimal("1") - tax_rate))
            tax = exit_price * quantity * tax_rate
            gross = (exit_price - entry) * quantity
            after = equity - Decimal("0.001")
            trade.update(
                planned_entry_price=str(entry),
                planned_exit_price=str(exit_price),
                entry_price=str(entry),
                exit_price=str(exit_price),
                gross_pnl_cash=str(gross),
                commission_cash=str(commission),
                net_pnl=str(net_pnl),
                planned_risk_cash=trade["planned_risk_cash"],
                net_r=str(
                    (
                        net_pnl / Decimal(str(trade["planned_risk_cash"]))
                    ).quantize(Decimal("1e-30"))
                ),
                setup_equity_before=str(equity),
                setup_equity_after=str(after),
            )
            equity = after

        with self.assertRaisesRegex(ValueError, r"sign.*equity"):
            self.evaluate_setup(history)

    def test_cost_reconciliation_tolerance_cannot_turn_exact_zero_positive(self):
        history = self.make_history("low_first_board", wins=50, losses=0, win_r=0)
        equity = Decimal("30000")
        for trade in history:
            entry = Decimal("10")
            quantity = Decimal("100")
            commission = Decimal("10")
            tax_rate = Decimal("0.0005")
            exit_price = (
                entry * quantity + commission
            ) / (quantity * (Decimal("1") - tax_rate))
            exact_tax = exit_price * quantity * tax_rate
            exact_gross = (exit_price - entry) * quantity
            reported_net = Decimal("0.005")
            planned_risk = Decimal(str(trade["planned_risk_cash"]))
            before = equity
            equity += reported_net
            trade.update(
                planned_entry_price=str(entry),
                planned_exit_price=str(exit_price),
                entry_price=str(entry),
                exit_price=str(exit_price),
                gross_pnl_cash=str(exact_gross + reported_net),
                commission_cash=str(commission),
                tax_cash=str(exact_tax),
                net_pnl=str(reported_net),
                planned_risk_cash=str(planned_risk),
                net_r=str(
                    (reported_net / planned_risk).quantize(Decimal("1e-30"))
                ),
                setup_equity_before=str(before),
                setup_equity_after=str(equity),
            )

        with self.assertRaisesRegex(ValueError, r"sign|zero|cost"):
            self.evaluate_setup(history)

    def test_stacked_cash_tolerances_cannot_turn_exact_loss_positive(self):
        history = self.make_history("low_first_board", wins=50, losses=0, win_r=0)
        equity = Decimal("30000")
        for trade in history:
            entry = Decimal("10")
            exit_price = Decimal("10.1049")
            quantity = Decimal("100")
            exact_gross = (exit_price - entry) * quantity
            reported_net = Decimal("0.005")
            planned_risk = Decimal(str(trade["planned_risk_cash"]))
            before = equity
            equity += reported_net
            trade.update(
                planned_entry_price=str(entry),
                planned_exit_price=str(exit_price),
                entry_price=str(entry),
                exit_price=str(exit_price),
                gross_pnl_cash=str(exact_gross + Decimal("0.01")),
                commission_cash="9.99",
                tax_cash="0.50",
                net_pnl=str(reported_net),
                planned_risk_cash=str(planned_risk),
                net_r=str(
                    (reported_net / planned_risk).quantize(Decimal("1e-30"))
                ),
                setup_equity_before=str(before),
                setup_equity_after=str(equity),
            )

        with self.assertRaisesRegex(ValueError, r"sign|cost|reconcile"):
            self.evaluate_setup(history)

    def test_slippage_attribution_must_reconcile_to_planned_and_fill_prices(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1]["slippage_cash"] = "10"

        with self.assertRaisesRegex(ValueError, r"slippage_cash.*reconcile"):
            self.evaluate_setup(history)

    def test_net_pnl_must_reconcile_to_setup_equity_curve(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1]["setup_equity_after"] = history[-1]["setup_equity_before"]

        with self.assertRaisesRegex(ValueError, r"net_pnl.*equity"):
            self.evaluate_setup(history)

    def test_zero_expectancy_does_not_unlock_upgrade(self):
        history = self.make_history(
            "low_first_board",
            wins=25,
            losses=25,
            win_r=1.0,
        )

        decision = self.evaluate_setup(history)

        self.assertEqual(decision["net_expectancy_r"], 0.0)
        self.assertEqual(decision["tier"], "validation")

    def test_future_dated_closed_trades_cannot_unlock_upgrade(self):
        history = self.make_history("low_first_board", wins=30, losses=20)
        history[-1]["exit_date"] = "2099-01-01"
        history[-1]["exit_timestamp"] = "2099-01-01T15:00:00+08:00"

        with self.assertRaisesRegex(ValueError, r"exit_date.*cutoff"):
            self.evaluate_setup(history)


class RiskTierConfigTests(CalcTradePlanTestCase):
    def test_validation_tier_config(self):
        config = self.calc.get_risk_tier_config("validation")

        self.assertEqual(
            config,
            {
                "single_risk": 0.0075,
                "portfolio_risk": 0.015,
                "main_board_cap": 0.30,
                "twenty_cm_cap": 0.15,
                "total_cap": 0.60,
                "single_gap_stress": 0.03,
            },
        )

    def test_upgrade_tier_config(self):
        config = self.calc.get_risk_tier_config("upgrade")

        self.assertEqual(
            config,
            {
                "single_risk": 0.015,
                "portfolio_risk": 0.02,
                "main_board_cap": 0.30,
                "twenty_cm_cap": 0.20,
                "total_cap": 0.60,
                "single_gap_stress": 0.04,
            },
        )


class RiskLockTests(CalcTradePlanTestCase):
    def test_daily_loss_of_1_5_percent_locks_new_positions(self):
        self.assertTrue(
            self.calc.daily_lock_triggered(
                daily_loss_fraction=0.015,
                stop_loss_count=1,
            )
        )

    def test_second_stop_loss_locks_new_positions(self):
        self.assertTrue(
            self.calc.daily_lock_triggered(
                daily_loss_fraction=0.01,
                stop_loss_count=2,
            )
        )

    def test_daily_limits_below_both_thresholds_do_not_lock(self):
        self.assertFalse(
            self.calc.daily_lock_triggered(
                daily_loss_fraction=0.0149,
                stop_loss_count=1,
            )
        )

    def test_weekly_five_percent_loss_locks_week_and_halves_next_five_days(self):
        action = self.calc.weekly_loss_action(weekly_loss_fraction=0.05)

        self.assertTrue(action["lock_remainder_of_week"])
        self.assertEqual(action["reduced_risk_trading_days"], 5)
        self.assertEqual(action["risk_multiplier"], 0.0)
        self.assertEqual(action["post_lock_risk_multiplier"], 0.5)

    def test_weekly_loss_below_five_percent_does_not_trigger_restrictions(self):
        action = self.calc.weekly_loss_action(weekly_loss_fraction=0.0499)

        self.assertFalse(action["lock_remainder_of_week"])
        self.assertEqual(action["reduced_risk_trading_days"], 0)
        self.assertEqual(action["risk_multiplier"], 1.0)

    def test_recovery_state_survives_the_week_boundary(self):
        action = self.calc.weekly_loss_action(
            weekly_loss_fraction=0.0,
            lock_remainder_of_week=False,
            reduced_risk_trading_days_remaining=5,
        )

        self.assertFalse(action["lock_remainder_of_week"])
        self.assertEqual(action["reduced_risk_trading_days"], 5)
        self.assertEqual(action["risk_multiplier"], 0.5)

    def test_persisted_week_lock_cannot_drop_the_five_recovery_days(self):
        with self.assertRaisesRegex(ValueError, r"lock.*remaining"):
            self.calc.weekly_loss_action(
                weekly_loss_fraction=0.0,
                lock_remainder_of_week=True,
                reduced_risk_trading_days_remaining=0,
            )


class JsonDispatchSafetyTests(CalcTradePlanTestCase):
    def test_mixed_nested_and_top_level_arguments_are_rejected(self):
        payload = {
            "operation": "daily_lock_triggered",
            "daily_loss_fraction": 0.02,
            "stop_loss_count": 2,
            "arguments": {
                "daily_loss_fraction": 0.0,
                "stop_loss_count": 0,
            },
        }

        with self.assertRaisesRegex(ValueError, r"mixed.*arguments"):
            self.calc.dispatch_json_request(payload)

    def test_timezone_qualified_clock_is_rejected(self):
        with self.assertRaisesRegex(ValueError, r"timezone"):
            self.calc.dispatch_json_request(
                {
                    "operation": "classify_auction_phase",
                    "current_time": "09:15:00+00:00",
                }
            )

    def test_main_returns_strict_json_error_for_extreme_number(self):
        request = io.StringIO(
            json.dumps(
                {
                    "operation": "calculate_overnight_notional_cap",
                    "equity": "1e309",
                    "limit_down_fraction": 0.1,
                    "tier": "validation",
                    "existing_single_stress_fraction": 0.0,
                    "existing_cluster_stress_fraction": 0.0,
                }
            )
        )
        output = io.StringIO()
        original_stdin = sys.stdin
        try:
            sys.stdin = request
            with redirect_stdout(output):
                exit_code = self.calc.main([])
        finally:
            sys.stdin = original_stdin

        response = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(response["ok"])
        self.assertNotIn("Infinity", output.getvalue())

    def test_main_rejects_duplicate_top_level_json_keys(self):
        request = io.StringIO(
            '{"operation":"daily_lock_triggered",'
            '"daily_loss_fraction":0.02,"daily_loss_fraction":0,'
            '"stop_loss_count":0}'
        )
        output = io.StringIO()
        original_stdin = sys.stdin
        try:
            sys.stdin = request
            with redirect_stdout(output):
                exit_code = self.calc.main([])
        finally:
            sys.stdin = original_stdin

        response = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(response["ok"])
        self.assertRegex(response["error"]["message"], r"duplicate JSON key")

    def test_main_rejects_duplicate_nested_argument_keys(self):
        request = io.StringIO(
            '{"operation":"daily_lock_triggered","arguments":{'
            '"daily_loss_fraction":0.02,"daily_loss_fraction":0,'
            '"stop_loss_count":0}}'
        )
        output = io.StringIO()
        original_stdin = sys.stdin
        try:
            sys.stdin = request
            with redirect_stdout(output):
                exit_code = self.calc.main([])
        finally:
            sys.stdin = original_stdin

        response = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(response["ok"])
        self.assertRegex(response["error"]["message"], r"duplicate JSON key")


if __name__ == "__main__":
    unittest.main()
