import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TARGET_SCRIPT = (
    WORKSPACE_ROOT
    / "a-share-ultrashort-preopen"
    / "scripts"
    / "calc_trade_plan.py"
)


def load_target_module():
    spec = importlib.util.spec_from_file_location("calc_trade_plan_e2e", TARGET_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load target module from {TARGET_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(TARGET_SCRIPT.is_file(), "calculator implementation missing")
class EndToEndGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calc = load_target_module()

    def base_inputs(self):
        return {
            "equity": 30_000.0,
            "cash": 30_000.0,
            "open_order_exposure": 0.0,
            "decision_date": "2026-09-01",
            "decision_time": "2026-09-01T09:26:00+08:00",
            "data_cutoff": "2026-09-01T09:25:00+08:00",
            "latest_completed_trading_date": "2026-08-31",
            "rule_version_checked_at": "2026-09-01",
            "rules_match_bundled_configuration": True,
            "trading_session_confirmed": True,
            "final_auction_data_confirmed": True,
            "final_auction_price": 10.0,
            "forbidden_chase_price": 10.3,
            "symbol": "600000",
            "exchange": "SSE",
            "entry_price": 10.0,
            "stop_price": 9.7,
            "target_price": 10.8,
            "index_state": "rising",
            "index_turning_up": False,
            "sentiment_state": "main_rise",
            "style_setup_match": "matched",
            "mainline_state": "confirmed",
            "setup": "low_first_board",
            "trade_history": [],
            "market_segment": "main_board",
            "existing_portfolio_risk": 0.0,
            "existing_total_notional": 0.0,
            "existing_symbol_notional": 0.0,
            "existing_symbol_stress_fraction": 0.0,
            "sector_notional_cap": 18_000.0,
            "limit_down_fraction": 0.10,
            "existing_cluster_stress_fraction": 0.0,
            "existing_ice_trial_plan_count": 0,
            "daily_loss_fraction": 0.0,
            "stop_loss_count": 0,
            "weekly_loss_fraction": 0.0,
            "weekly_lock_remainder_of_week": False,
            "weekly_recovery_days_remaining": 0,
            "commission_rate": 0.0003,
            "minimum_commission": 5.0,
            "sell_tax_rate": 0.0005,
            "slippage_rate_per_side": 0.001,
            "additional_fee_rate_per_side": 0.0,
            "commission_basis": "all_in",
        }

    @staticmethod
    def upgrade_history(exit_date="2026-08-31"):
        history = []
        equity = Decimal("30000")
        entry = Decimal("10")
        stop = Decimal("9.90")
        quantity = Decimal("100")
        commission_rate = Decimal("0.0003")
        minimum_commission = Decimal("5")
        tax_rate = Decimal("0.0005")
        planned_slippage = Decimal("0.0005")
        buy_notional = entry * (Decimal("1") + planned_slippage) * quantity
        stop_notional = stop * (Decimal("1") - planned_slippage) * quantity
        planned_risk = (
            buy_notional
            + max(minimum_commission, buy_notional * commission_rate)
            - (
                stop_notional
                - max(minimum_commission, stop_notional * commission_rate)
                - stop_notional * tax_rate
            )
        )
        for index in range(50):
            equity_before = equity
            net_pnl = Decimal("1")
            equity += net_pnl
            commission_cash = Decimal("10")
            exit_price = (
                net_pnl + entry * quantity + commission_cash
            ) / (quantity * (Decimal("1") - tax_rate))
            tax_cash = exit_price * quantity * tax_rate
            gross_pnl = (exit_price - entry) * quantity
            history.append(
                {
                    "trade_id": f"low-first-board-{index}",
                    "exit_date": exit_date,
                    "exit_timestamp": f"{exit_date}T14:{index:02d}:00+08:00",
                    "trading_session_confirmed": True,
                    "trading_session_evidence_id": f"calendar-{exit_date}",
                    "setup": "low_first_board",
                    "symbol": "600000",
                    "exchange": "SSE",
                    "planned_entry_price": str(entry),
                    "planned_stop_price": str(stop),
                    "planned_exit_price": str(exit_price),
                    "entry_price": str(entry),
                    "exit_price": str(exit_price),
                    "shares": 100,
                    "gross_pnl_cash": str(gross_pnl),
                    "commission_cash": str(commission_cash),
                    "tax_cash": str(tax_cash),
                    "additional_fees_cash": "0",
                    "commission_rate": str(commission_rate),
                    "minimum_commission": str(minimum_commission),
                    "commission_basis": "all_in",
                    "additional_fee_rate_per_side": "0",
                    "sell_tax_rate": str(tax_rate),
                    "planned_slippage_rate_per_side": str(planned_slippage),
                    "slippage_cash": "0",
                    "net_pnl": str(net_pnl),
                    "planned_risk_cash": str(planned_risk),
                    "net_r": str(net_pnl / planned_risk),
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

    def test_approved_trade_uses_fixed_validation_budget_and_caps(self):
        result = self.calc.evaluate_new_position_plan(**self.base_inputs())

        self.assertEqual(result["decision"], "trade")
        self.assertEqual(result["risk_budget"], 225.0)
        self.assertGreater(result["recommended_shares"], 0)
        self.assertEqual(result["recommended_shares"] % 100, 0)
        self.assertLessEqual(result["position_notional"], 9_000.0)
        self.assertGreaterEqual(result["net_reward_risk"], 2.0)

    def test_defense_environment_atomically_rejects_new_position(self):
        inputs = self.base_inputs()
        inputs["sentiment_state"] = "retreat"

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["recommended_shares"], 0)
        self.assertIn("ENVIRONMENT_DEFENSE", result["reason_codes"])

    def test_caller_cannot_understate_segment_limit_down_stress(self):
        inputs = self.base_inputs()
        inputs.update(
            symbol="300001",
            exchange="SZSE",
            market_segment="chinext",
            limit_down_fraction=0.01,
            existing_total_notional=6_000.0,
            existing_cluster_stress_fraction=0.04,
        )

        with self.assertRaisesRegex(ValueError, r"limit_down_fraction.*chinext"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_symbol_code_prevents_twenty_cm_board_from_masquerading_as_main(self):
        cases = (
            ("300001", "SZSE"),
            ("301001", "SZSE"),
            ("688001", "SSE"),
            ("689001", "SSE"),
        )
        for symbol, exchange in cases:
            inputs = self.base_inputs()
            inputs.update(symbol=symbol, exchange=exchange)
            with self.subTest(symbol=symbol):
                with self.assertRaisesRegex(ValueError, r"market_segment.*symbol"):
                    self.calc.evaluate_new_position_plan(**inputs)

    def test_exchange_must_match_the_symbol_code(self):
        inputs = self.base_inputs()
        inputs["exchange"] = "SZSE"

        with self.assertRaisesRegex(ValueError, r"exchange.*symbol"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_rule_check_must_be_fresh_for_the_decision_date(self):
        inputs = self.base_inputs()
        inputs.update(
            decision_date="2026-09-02",
            decision_time="2026-09-02T09:26:00+08:00",
            data_cutoff="2026-09-02T09:25:00+08:00",
            latest_completed_trading_date="2026-09-01",
            rule_version_checked_at="2026-09-01",
        )

        with self.assertRaisesRegex(ValueError, r"rule_version_checked_at.*decision"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_bundled_rule_snapshot_cannot_be_used_for_an_earlier_plan_date(self):
        inputs = self.base_inputs()
        inputs.update(
            decision_date="2025-01-02",
            decision_time="2025-01-02T09:26:00+08:00",
            data_cutoff="2025-01-02T09:25:00+08:00",
            latest_completed_trading_date="2024-12-31",
            rule_version_checked_at="2025-01-02",
        )

        with self.assertRaisesRegex(ValueError, r"bundled rule|2026-09-01"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_weekend_cannot_be_attested_as_a_trading_session(self):
        inputs = self.base_inputs()
        inputs.update(
            decision_date="2026-09-05",
            decision_time="2026-09-05T09:26:00+08:00",
            data_cutoff="2026-09-05T09:25:00+08:00",
            latest_completed_trading_date="2026-09-04",
            rule_version_checked_at="2026-09-05",
        )

        with self.assertRaisesRegex(ValueError, r"weekend|trading session"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_exact_plan_requires_a_strict_trading_session_attestation(self):
        for value in (False, 1, "true"):
            inputs = self.base_inputs()
            inputs["trading_session_confirmed"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    (TypeError, ValueError),
                    r"trading_session_confirmed",
                ):
                    self.calc.evaluate_new_position_plan(**inputs)

    def test_exact_plan_is_rejected_before_final_call_auction_data_exists(self):
        inputs = self.base_inputs()
        inputs.update(
            decision_time="2026-09-01T09:24:59+08:00",
            data_cutoff="2026-09-01T09:24:59+08:00",
            final_auction_data_confirmed=False,
        )

        with self.assertRaisesRegex(ValueError, r"09:25|final auction"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_post_auction_exact_plan_rejects_stale_market_data(self):
        inputs = self.base_inputs()
        inputs["data_cutoff"] = "2026-08-31T15:00:00+08:00"

        with self.assertRaisesRegex(ValueError, r"data_cutoff|09:25"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_final_auction_above_forbidden_chase_price_cancels_trade(self):
        inputs = self.base_inputs()
        inputs.update(
            final_auction_price=10.4,
            forbidden_chase_price=10.3,
        )

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(
            result["reason_codes"],
            ["FINAL_AUCTION_ABOVE_CHASE_LIMIT"],
        )
        self.assertEqual(result["recommended_shares"], 0)

    def test_final_auction_below_chase_still_reprices_net_r_conservatively(self):
        inputs = self.base_inputs()
        inputs.update(
            entry_price=10.0,
            final_auction_price=10.2,
            forbidden_chase_price=10.3,
        )

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["reason_codes"], ["NET_R_BELOW_THRESHOLD"])
        self.assertEqual(result["planned_entry_price"], 10.0)
        self.assertEqual(result["effective_entry_price"], 10.2)
        self.assertLess(result["net_reward_risk"], result["minimum_net_r"])

    def test_final_auction_at_or_below_stop_invalidates_the_trade(self):
        for auction_price in (9.70, 9.69):
            inputs = self.base_inputs()
            inputs["final_auction_price"] = auction_price

            with self.subTest(final_auction_price=auction_price):
                result = self.calc.evaluate_new_position_plan(**inputs)

                self.assertEqual(result["decision"], "no_trade")
                self.assertEqual(
                    result["reason_codes"],
                    ["FINAL_AUCTION_INVALIDATES_PRICE_STRUCTURE"],
                )
                self.assertEqual(result["recommended_shares"], 0)

    def test_existing_ice_trial_plan_blocks_a_second_trial(self):
        inputs = self.base_inputs()
        inputs.update(
            index_state="falling",
            index_turning_up=True,
            sentiment_state="icepoint_repair",
            mainline_state="low_resonance",
            target_price=11.5,
            existing_ice_trial_plan_count=1,
        )

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["environment_state"], "ice_trial")
        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(
            result["reason_codes"],
            ["ICE_TRIAL_PLAN_SLOT_OCCUPIED"],
        )

    def test_rule_configuration_attestation_must_be_strict_true_boolean(self):
        for value in (False, 1, "true"):
            inputs = self.base_inputs()
            inputs["rules_match_bundled_configuration"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex((TypeError, ValueError), r"rules_match"):
                    self.calc.evaluate_new_position_plan(**inputs)

    def test_exact_plan_rejects_zero_friction_assumptions(self):
        inputs = self.base_inputs()
        inputs.update(
            commission_rate=0,
            minimum_commission=0,
            sell_tax_rate=0,
            slippage_rate_per_side=0,
        )

        with self.assertRaisesRegex(ValueError, r"cost|commission|tax|slippage"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_tiny_positive_values_cannot_impersonate_real_friction(self):
        inputs = self.base_inputs()
        inputs.update(
            commission_rate="1e-100",
            minimum_commission=0,
            slippage_rate_per_side="1e-100",
            target_price=10.22,
        )

        with self.assertRaisesRegex(ValueError, r"commission|slippage|precision"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_minimum_fee_cannot_replace_a_real_commission_rate(self):
        inputs = self.base_inputs()
        inputs.update(
            equity=1_000_000.0,
            cash=1_000_000.0,
            commission_rate=0,
            minimum_commission=5.0,
            sector_notional_cap=600_000.0,
            target_price=10.68,
        )

        with self.assertRaisesRegex(ValueError, r"commission.*rate floor"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_overprecision_money_cannot_round_through_a_hard_cap(self):
        inputs = self.base_inputs()
        inputs.update(
            equity="5004." + "9" * 120,
            cash="5004." + "9" * 120,
            existing_cluster_stress_fraction=0.03,
            stop_price=9.90,
            target_price=10.60,
        )

        with self.assertRaisesRegex(ValueError, r"equity.*precision"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_same_day_closed_history_cannot_upgrade_a_preopen_plan(self):
        inputs = self.base_inputs()
        inputs["trade_history"] = self.upgrade_history(exit_date="2026-09-01")

        with self.assertRaisesRegex(ValueError, r"completed trading date|cutoff"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_latest_completed_trading_date_must_precede_plan_date(self):
        inputs = self.base_inputs()
        inputs["latest_completed_trading_date"] = inputs["decision_date"]

        with self.assertRaisesRegex(ValueError, r"earlier than decision_date"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_exact_plan_rejects_price_off_the_a_share_tick(self):
        inputs = self.base_inputs()
        inputs["target_price"] = "10.599"

        with self.assertRaisesRegex(ValueError, r"target_price.*tick"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_same_symbol_stress_must_be_inside_cluster_stress(self):
        inputs = self.base_inputs()
        inputs.update(
            existing_total_notional=6_000.0,
            existing_symbol_notional=6_000.0,
            existing_symbol_stress_fraction=0.02,
            existing_cluster_stress_fraction=0.01,
        )

        with self.assertRaisesRegex(ValueError, r"symbol.*cluster"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_same_symbol_stress_must_reconcile_to_notional(self):
        inputs = self.base_inputs()
        inputs.update(
            existing_total_notional=3_000.0,
            existing_symbol_notional=3_000.0,
            existing_symbol_stress_fraction=0.0,
            existing_cluster_stress_fraction=0.01,
        )

        with self.assertRaisesRegex(ValueError, r"symbol.*stress.*reconcile"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_end_to_end_does_not_round_trip_overnight_cap_through_float(self):
        inputs = self.base_inputs()
        inputs["existing_cluster_stress_fraction"] = 0.049

        with mock.patch.object(
            self.calc,
            "calculate_overnight_notional_cap",
            return_value=1_000_000.0,
        ):
            result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("ONE_LOT_EXCEEDS_CAPS", result["reason_codes"])

    def test_unknown_axis_atomically_rejects_exact_position(self):
        inputs = self.base_inputs()
        inputs["index_state"] = "unknown"

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["recommended_shares"], 0)
        self.assertIn("ENVIRONMENT_INSUFFICIENT", result["reason_codes"])

    def test_falling_market_non_icepoint_repair_rejects_even_three_r(self):
        inputs = self.base_inputs()
        inputs.update(
            index_state="falling",
            sentiment_state="repair",
            target_price=11.2,
        )

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("ENVIRONMENT_DEFENSE", result["reason_codes"])

    def test_net_r_below_environment_threshold_atomically_rejects(self):
        inputs = self.base_inputs()
        inputs["target_price"] = 10.5

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["recommended_shares"], 0)
        self.assertIn("NET_R_BELOW_THRESHOLD", result["reason_codes"])

    def test_overprecision_cannot_round_up_through_net_r_gate(self):
        inputs = self.base_inputs()
        inputs["target_price"] = "10.5" + "9" * 120

        with self.assertRaisesRegex(ValueError, r"target_price.*tick"):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_daily_lock_atomically_rejects_new_position(self):
        inputs = self.base_inputs()
        inputs["daily_loss_fraction"] = 0.015

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("DAILY_NEW_POSITION_LOCK", result["reason_codes"])

    def test_weekly_lock_atomically_rejects_new_position(self):
        inputs = self.base_inputs()
        inputs["weekly_loss_fraction"] = 0.05

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("WEEKLY_NEW_POSITION_LOCK", result["reason_codes"])

    def test_following_five_trading_days_halve_current_risk_tier(self):
        normal = self.calc.evaluate_new_position_plan(**self.base_inputs())
        inputs = self.base_inputs()
        inputs["weekly_recovery_days_remaining"] = 5

        reduced = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(normal["risk_budget"], 225.0)
        self.assertEqual(reduced["risk_budget"], 112.5)
        self.assertLessEqual(
            reduced["recommended_shares"],
            normal["recommended_shares"],
        )

    def test_existing_portfolio_risk_can_leave_less_than_one_lot(self):
        inputs = self.base_inputs()
        inputs["existing_portfolio_risk"] = 440.0

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["recommended_shares"], 0)
        self.assertIn("ONE_LOT_EXCEEDS_CAPS", result["reason_codes"])

    def test_open_order_exposure_is_removed_from_available_cash(self):
        inputs = self.base_inputs()
        inputs["open_order_exposure"] = 29_500.0

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["recommended_shares"], 0)
        self.assertIn("OPEN_ORDER_EXPOSURE_UNRESOLVED", result["reason_codes"])

    def test_any_unresolved_open_order_blocks_an_exact_new_position(self):
        inputs = self.base_inputs()
        inputs["open_order_exposure"] = 1_000.0

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertIn("OPEN_ORDER_EXPOSURE_UNRESOLVED", result["reason_codes"])

    def test_existing_same_symbol_position_reduces_single_stock_caps(self):
        inputs = self.base_inputs()
        inputs.update(
            existing_total_notional=9_000.0,
            existing_symbol_notional=9_000.0,
            existing_symbol_stress_fraction=0.03,
            existing_cluster_stress_fraction=0.03,
        )

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["recommended_shares"], 0)

    def test_twenty_cm_validation_single_stock_cap_is_fifteen_percent(self):
        inputs = self.base_inputs()
        inputs.update(
            symbol="688001",
            exchange="SSE",
            market_segment="star",
            limit_down_fraction=0.20,
            stop_price=9.9,
            target_price=10.5,
        )

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "trade")
        self.assertLessEqual(result["position_notional"], 4_500.0)
        self.assertGreaterEqual(result["recommended_shares"], 200)

    def test_caller_cannot_override_the_market_order_grid(self):
        inputs = self.base_inputs()
        inputs.update(minimum_order_shares=1, share_increment=1)

        with self.assertRaises(TypeError):
            self.calc.evaluate_new_position_plan(**inputs)

    def test_one_lot_over_risk_budget_is_no_trade(self):
        inputs = self.base_inputs()
        inputs.update(
            entry_price=100.0,
            stop_price=95.0,
            target_price=112.0,
            final_auction_price=100.0,
            forbidden_chase_price=101.0,
        )

        result = self.calc.evaluate_new_position_plan(**inputs)

        self.assertEqual(result["decision"], "no_trade")
        self.assertEqual(result["recommended_shares"], 0)
        self.assertIn("ONE_LOT_EXCEEDS_CAPS", result["reason_codes"])

    def test_cli_does_not_expose_unsafe_low_level_position_sizing(self):
        with self.assertRaisesRegex(ValueError, r"unknown operation"):
            self.calc.dispatch_json_request(
                {
                    "operation": "calculate_position_shares",
                    "cash": 30_000,
                    "entry_price": 10,
                    "stop_price": 9.5,
                    "risk_budget": 30_000,
                    "stage_notional_cap": 30_000,
                    "board_notional_cap": 30_000,
                    "overnight_notional_cap": 30_000,
                    "minimum_order_shares": 100,
                    "share_increment": 100,
                    "commission_rate": 0,
                    "minimum_commission": 0,
                    "sell_tax_rate": 0,
                    "slippage_rate_per_side": 0,
                    "additional_fee_rate_per_side": 0,
                    "commission_basis": "all_in",
                }
            )

    def test_caller_cannot_directly_request_upgrade_tier(self):
        inputs = self.base_inputs()
        inputs["risk_tier"] = "upgrade"

        with self.assertRaises(TypeError):
            self.calc.evaluate_new_position_plan(**inputs)


if __name__ == "__main__":
    unittest.main()
