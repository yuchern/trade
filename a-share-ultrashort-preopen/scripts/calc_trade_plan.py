#!/usr/bin/env python3
"""Deterministic A-share pre-open trade-plan calculations.

The module exposes small, side-effect-free functions for use by tests and other
local tooling.  Its command-line interface reads one JSON request from a file
or standard input and writes one JSON response.  It does not access the network
or place orders.
"""

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Optional


_ZERO = Decimal("0")
_ONE = Decimal("1")
_MAX_STRUCTURAL_STOP = Decimal("0.05")
_MIN_UPGRADE_TRADES = 50
_MIN_UPGRADE_DISCIPLINE = Decimal("0.90")
_MAX_UPGRADE_DRAWDOWN = Decimal("0.08")
_MAX_CLUSTER_GAP_STRESS = Decimal("0.05")
_CASH_RECONCILIATION_TOLERANCE = Decimal("0.01")
_R_RECONCILIATION_TOLERANCE = Decimal("0.000001")
_STRESS_RECONCILIATION_TOLERANCE = Decimal("0.000000000001")
_ARITHMETIC_ZERO_TOLERANCE = Decimal("1e-24")
_PRICE_TICK = Decimal("0.01")
_MINIMUM_SELL_TAX_RATE = Decimal("0.0005")
_MINIMUM_SLIPPAGE_RATE = Decimal("0.0005")
_MAXIMUM_PLANNED_SLIPPAGE_RATE = Decimal("0.05")
_MINIMUM_COMMISSION_PER_SIDE = Decimal("5.00")
_MINIMUM_ROUND_TRIP_COMMISSION = Decimal("10.00")
_MAXIMUM_COMMISSION_RATE = Decimal("0.003")
# 0.002% securities-management fee + 0.00341% handling fee + 0.001%
# transfer fee.  An all-in commission, or net commission plus separately
# declared fees, must cover this unavoidable per-side rate floor.
_MINIMUM_ALL_IN_FEE_RATE = Decimal("0.0000641")
_LOCAL_DECIMAL_PRECISION = 80
_MAX_INPUT_SIGNIFICANT_DIGITS = 30
_MAX_INPUT_INTEGER_DIGITS = 18
_MAX_INPUT_DECIMAL_PLACES = 30
_MAX_FRACTION_DECIMAL_PLACES = 12
_MAX_INTEGER_INPUT = 10**15
_BUNDLED_RULES_AS_OF = date(2026, 9, 1)
_SHANGHAI_UTC_OFFSET = timedelta(hours=8)

_RISK_TIER_CONFIGS = {
    "validation": {
        "single_risk": 0.0075,
        "portfolio_risk": 0.015,
        "main_board_cap": 0.30,
        "twenty_cm_cap": 0.15,
        "total_cap": 0.60,
        "single_gap_stress": 0.03,
    },
    "upgrade": {
        "single_risk": 0.015,
        "portfolio_risk": 0.02,
        "main_board_cap": 0.30,
        "twenty_cm_cap": 0.20,
        "total_cap": 0.60,
        "single_gap_stress": 0.04,
    },
}

_ENVIRONMENT_POLICIES = {
    "attack": {"total_cap": 0.60, "minimum_net_r": 2.0},
    "cautious": {"total_cap": 0.30, "minimum_net_r": 2.5},
    "defense": {"total_cap": 0.0, "minimum_net_r": None},
    "ice_trial": {"total_cap": 0.10, "minimum_net_r": 3.0},
}

# Verified for the in-scope normal A-share segments at as_of=2026-09-01.
# Revalidate official rules before changing the skill's as_of date.
_MARKET_SEGMENT_RULES = {
    "main_board": {
        "board_cap_key": "main_board_cap",
        "minimum_order_shares": 100,
        "share_increment": 100,
        "limit_down_fraction": Decimal("0.10"),
    },
    "chinext": {
        "board_cap_key": "twenty_cm_cap",
        "minimum_order_shares": 100,
        "share_increment": 100,
        "limit_down_fraction": Decimal("0.20"),
    },
    "star": {
        "board_cap_key": "twenty_cm_cap",
        "minimum_order_shares": 200,
        "share_increment": 1,
        "limit_down_fraction": Decimal("0.20"),
    },
}


def _uses_local_decimal_context(function):
    """Keep precision local so importing this module cannot alter its caller."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with localcontext() as context:
            context.prec = _LOCAL_DECIMAL_PRECISION
            return function(*args, **kwargs)

    return wrapped


def _as_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a finite number, not bool")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be a finite number") from exc
    if not number.is_finite():
        raise ValueError(f"{name} must be finite")
    normalized = number.normalize() if number != _ZERO else _ZERO
    significant_digits = len(normalized.as_tuple().digits)
    integer_digits = max(0, normalized.adjusted() + 1) if number != _ZERO else 1
    decimal_places = max(0, -normalized.as_tuple().exponent)
    if (
        significant_digits > _MAX_INPUT_SIGNIFICANT_DIGITS
        or integer_digits > _MAX_INPUT_INTEGER_DIGITS
        or decimal_places > _MAX_INPUT_DECIMAL_PLACES
    ):
        raise ValueError(f"{name} exceeds supported numeric precision")
    return number


def _non_negative_decimal(value: Any, name: str) -> Decimal:
    number = _as_decimal(value, name)
    if number < _ZERO:
        raise ValueError(f"{name} must be non-negative")
    return number


def _positive_decimal(value: Any, name: str) -> Decimal:
    number = _as_decimal(value, name)
    if number <= _ZERO:
        raise ValueError(f"{name} must be greater than zero")
    return number


def _non_negative_money(value: Any, name: str) -> Decimal:
    number = _non_negative_decimal(value, name)
    if number % _PRICE_TICK != _ZERO:
        raise ValueError(f"{name} exceeds supported currency precision of 0.01")
    return number


def _positive_money(value: Any, name: str) -> Decimal:
    number = _non_negative_money(value, name)
    if number == _ZERO:
        raise ValueError(f"{name} must be greater than zero")
    return number


def _non_negative_fraction(value: Any, name: str) -> Decimal:
    number = _non_negative_decimal(value, name)
    normalized = number.normalize() if number != _ZERO else _ZERO
    if max(0, -normalized.as_tuple().exponent) > _MAX_FRACTION_DECIMAL_PLACES:
        raise ValueError(f"{name} exceeds supported fraction precision")
    return number


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    if value > _MAX_INTEGER_INPUT:
        raise ValueError(f"{name} exceeds the supported integer range")
    return value


def _positive_int(value: Any, name: str) -> int:
    integer = _non_negative_int(value, name)
    if integer == 0:
        raise ValueError(f"{name} must be greater than zero")
    return integer


def _as_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


def _finite_float(value: Decimal, name: str) -> float:
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} is outside the supported numeric range") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} is outside the supported numeric range")
    return result


def _decimal_sign(value: Decimal) -> int:
    """Return a stable sign without promoting tiny Decimal division residue."""

    if abs(value) <= _ARITHMETIC_ZERO_TOLERANCE:
        return 0
    return 1 if value > _ZERO else -1


def _iso_date(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _iso_shanghai_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO datetime with +08:00 offset")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an ISO datetime with +08:00 offset"
        ) from exc
    if parsed.utcoffset() != _SHANGHAI_UTC_OFFSET:
        raise ValueError(f"{name} must use the Asia/Shanghai +08:00 offset")
    return parsed


def _derive_market_segment(symbol: Any, exchange: Any) -> tuple[str, str, str]:
    """Derive the in-scope board from a canonical six-digit A-share code."""

    if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("symbol must be a canonical six-digit A-share code")
    normalized_exchange = _normalized_choice(
        exchange,
        "exchange",
        {"sse", "szse"},
    )
    prefix = symbol[:3]
    if prefix in {"600", "601", "603", "605"}:
        derived_exchange, segment = "sse", "main_board"
    elif prefix in {"688", "689"}:
        derived_exchange, segment = "sse", "star"
    elif prefix in {"000", "001", "002", "003", "004"}:
        derived_exchange, segment = "szse", "main_board"
    elif 300 <= int(prefix) <= 309:
        derived_exchange, segment = "szse", "chinext"
    else:
        raise ValueError(
            "symbol is outside the bundled ordinary A-share code ranges"
        )
    if normalized_exchange != derived_exchange:
        raise ValueError("exchange does not match the canonical symbol code")
    return symbol, normalized_exchange, segment


def _price_on_tick(value: Any, name: str) -> Decimal:
    price = _positive_decimal(value, name)
    if price % _PRICE_TICK != _ZERO:
        raise ValueError(f"{name} must be on the A-share 0.01 price tick")
    return price


def _validated_cost_parameters(
    commission_rate: Any,
    minimum_commission: Any,
    sell_tax_rate: Any,
    slippage_rate_per_side: Any,
    additional_fee_rate_per_side: Any,
    commission_basis: Any,
):
    commission = _non_negative_fraction(commission_rate, "commission_rate")
    minimum = _non_negative_money(minimum_commission, "minimum_commission")
    sell_tax = _non_negative_fraction(sell_tax_rate, "sell_tax_rate")
    slippage = _non_negative_fraction(
        slippage_rate_per_side,
        "slippage_rate_per_side",
    )
    additional_fee = _non_negative_fraction(
        additional_fee_rate_per_side,
        "additional_fee_rate_per_side",
    )
    if not isinstance(commission_basis, str):
        raise TypeError("commission_basis must be a string")
    normalized_basis = commission_basis.strip().lower()
    if normalized_basis not in {"all_in", "net"}:
        raise ValueError("commission_basis must be 'all_in' or 'net'")
    if commission > _MAXIMUM_COMMISSION_RATE:
        raise ValueError(
            "commission_rate exceeds the in-scope A-share upper limit of 0.003"
        )
    if sell_tax >= _ONE:
        raise ValueError("sell_tax_rate must be less than 1")
    if slippage >= _ONE:
        raise ValueError("slippage_rate_per_side must be less than 1")
    if additional_fee >= _ONE:
        raise ValueError("additional_fee_rate_per_side must be less than 1")
    if normalized_basis == "all_in" and additional_fee != _ZERO:
        raise ValueError(
            "all_in commission_basis requires additional_fee_rate_per_side=0"
        )
    if normalized_basis == "net" and additional_fee == _ZERO:
        raise ValueError(
            "net commission_basis requires an explicit positive additional fee rate"
        )
    return commission, minimum, sell_tax, slippage, additional_fee


def _require_realistic_exact_plan_costs(
    *,
    commission: Decimal,
    minimum: Decimal,
    sell_tax: Decimal,
    slippage: Decimal,
    additional_fee: Decimal,
) -> None:
    """Reject zero or understated friction assumptions at the trading endpoint."""

    if minimum < _MINIMUM_COMMISSION_PER_SIDE:
        raise ValueError(
            "minimum_commission is below the in-scope A-share planning floor "
            "verified as of 2026-09-01"
        )
    if commission + additional_fee < _MINIMUM_ALL_IN_FEE_RATE:
        raise ValueError(
            "commission plus separately declared fees is below the in-scope "
            "A-share all-in rate floor verified as of 2026-09-01"
        )
    if sell_tax < _MINIMUM_SELL_TAX_RATE:
        raise ValueError(
            "sell_tax_rate is below the in-scope A-share rule floor "
            "verified as of 2026-09-01"
        )
    if slippage < _MINIMUM_SLIPPAGE_RATE:
        raise ValueError(
            "slippage_rate_per_side is below the conservative exact-plan floor"
        )


def _charged_commission(
    notional: Decimal,
    commission_rate: Decimal,
    minimum_commission: Decimal,
) -> Decimal:
    return max(minimum_commission, notional * commission_rate)


def _buy_cash_out(
    price: Decimal,
    quantity: Decimal,
    commission_rate: Decimal,
    minimum_commission: Decimal,
    slippage_rate_per_side: Decimal,
    additional_fee_rate_per_side: Decimal,
) -> Decimal:
    notional = price * (_ONE + slippage_rate_per_side) * quantity
    return (
        notional
        + _charged_commission(notional, commission_rate, minimum_commission)
        + notional * additional_fee_rate_per_side
    )


def _sell_cash_in(
    price: Decimal,
    quantity: Decimal,
    commission_rate: Decimal,
    minimum_commission: Decimal,
    sell_tax_rate: Decimal,
    slippage_rate_per_side: Decimal,
    additional_fee_rate_per_side: Decimal,
) -> Decimal:
    notional = price * (_ONE - slippage_rate_per_side) * quantity
    return (
        notional
        - _charged_commission(notional, commission_rate, minimum_commission)
        - notional * sell_tax_rate
        - notional * additional_fee_rate_per_side
    )


def _calculate_net_reward_risk_decimal(
    *,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    quantity: Decimal,
    commission: Decimal,
    minimum: Decimal,
    sell_tax: Decimal,
    slippage: Decimal,
    additional_fee: Decimal,
) -> Decimal:
    buy_cash_out = _buy_cash_out(
        entry,
        quantity,
        commission,
        minimum,
        slippage,
        additional_fee,
    )
    target_cash_in = _sell_cash_in(
        target,
        quantity,
        commission,
        minimum,
        sell_tax,
        slippage,
        additional_fee,
    )
    stop_cash_in = _sell_cash_in(
        stop,
        quantity,
        commission,
        minimum,
        sell_tax,
        slippage,
        additional_fee,
    )
    net_reward = target_cash_in - buy_cash_out
    net_risk = buy_cash_out - stop_cash_in
    if net_risk <= _ZERO:
        raise ValueError("net risk must be greater than zero after costs")
    return net_reward / net_risk


def classify_auction_phase(current_time: time) -> str:
    """Classify a local exchange time around the opening call auction."""

    if not isinstance(current_time, time):
        raise TypeError("current_time must be a datetime.time instance")
    if current_time.utcoffset() is not None:
        raise ValueError(
            "current_time must be an unqualified Asia/Shanghai exchange clock; "
            "timezone-qualified values are not accepted"
        )

    clock = (
        current_time.hour,
        current_time.minute,
        current_time.second,
        current_time.microsecond,
    )
    if clock < (9, 15, 0, 0):
        return "before_call_auction"
    if clock < (9, 20, 0, 0):
        return "cancelable_call_auction"
    if clock < (9, 25, 0, 0):
        return "non_cancelable_call_auction"
    return "post_call_auction"


@_uses_local_decimal_context
def calculate_net_reward_risk(
    *,
    entry_price: Any,
    stop_price: Any,
    target_price: Any,
    shares: int,
    commission_rate: Any,
    minimum_commission: Any,
    sell_tax_rate: Any,
    slippage_rate_per_side: Any,
    additional_fee_rate_per_side: Any,
    commission_basis: Any,
) -> float:
    """Return net reward/risk after round-trip costs and adverse slippage."""

    entry = _positive_decimal(entry_price, "entry_price")
    stop = _positive_decimal(stop_price, "stop_price")
    target = _positive_decimal(target_price, "target_price")
    quantity = Decimal(_positive_int(shares, "shares"))
    (
        commission,
        minimum,
        sell_tax,
        slippage,
        additional_fee,
    ) = _validated_cost_parameters(
        commission_rate,
        minimum_commission,
        sell_tax_rate,
        slippage_rate_per_side,
        additional_fee_rate_per_side,
        commission_basis,
    )
    _require_realistic_exact_plan_costs(
        commission=commission,
        minimum=minimum,
        sell_tax=sell_tax,
        slippage=slippage,
        additional_fee=additional_fee,
    )

    if not stop < entry < target:
        raise ValueError("prices must satisfy stop_price < entry_price < target_price")

    reward_risk = _calculate_net_reward_risk_decimal(
        entry=entry,
        stop=stop,
        target=target,
        quantity=quantity,
        commission=commission,
        minimum=minimum,
        sell_tax=sell_tax,
        slippage=slippage,
        additional_fee=additional_fee,
    )
    return _finite_float(reward_risk, "net_reward_risk")


@_uses_local_decimal_context
def calculate_break_even_win_rate(*, net_reward_risk: Any) -> float:
    """Return the win rate at which a one-loss-unit system breaks even."""

    reward_risk = _positive_decimal(net_reward_risk, "net_reward_risk")
    return _finite_float(
        _ONE / (_ONE + reward_risk),
        "break_even_win_rate",
    )


@_uses_local_decimal_context
def calculate_position_shares(
    *,
    cash: Any,
    entry_price: Any,
    stop_price: Any,
    risk_budget: Any,
    stage_notional_cap: Any,
    board_notional_cap: Any,
    overnight_notional_cap: Any,
    minimum_order_shares: int,
    share_increment: int,
    commission_rate: Any,
    minimum_commission: Any,
    sell_tax_rate: Any,
    slippage_rate_per_side: Any,
    additional_fee_rate_per_side: Any,
    commission_basis: Any,
) -> int:
    """Select the smallest cap and round to the security's legal share grid."""

    available_cash = _non_negative_decimal(cash, "cash")
    entry = _positive_decimal(entry_price, "entry_price")
    stop = _positive_decimal(stop_price, "stop_price")
    risk = _non_negative_decimal(risk_budget, "risk_budget")
    stage_cap = _non_negative_decimal(stage_notional_cap, "stage_notional_cap")
    board_cap = _non_negative_decimal(board_notional_cap, "board_notional_cap")
    overnight_cap = _non_negative_decimal(
        overnight_notional_cap,
        "overnight_notional_cap",
    )
    minimum_shares = _positive_int(
        minimum_order_shares,
        "minimum_order_shares",
    )
    increment = _positive_int(share_increment, "share_increment")
    (
        commission,
        minimum,
        sell_tax,
        slippage,
        additional_fee,
    ) = _validated_cost_parameters(
        commission_rate,
        minimum_commission,
        sell_tax_rate,
        slippage_rate_per_side,
        additional_fee_rate_per_side,
        commission_basis,
    )

    if stop >= entry:
        raise ValueError("stop_price must be below entry_price")

    per_share_risk = entry - stop
    structural_stop_fraction = per_share_risk / entry
    if structural_stop_fraction > _MAX_STRUCTURAL_STOP:
        raise ValueError("structural stop distance exceeds 5%")

    worst_entry_price = entry * (_ONE + slippage)
    share_caps = (
        risk / per_share_risk,
        stage_cap / worst_entry_price,
        board_cap / worst_entry_price,
        overnight_cap / worst_entry_price,
        available_cash / worst_entry_price,
    )
    raw_shares = min(share_caps)
    maximum_shares = int(raw_shares.to_integral_value(rounding=ROUND_FLOOR))
    if maximum_shares < minimum_shares:
        return 0
    maximum_steps = (maximum_shares - minimum_shares) // increment

    def shares_for_step(step_count: int) -> int:
        return minimum_shares + step_count * increment

    def within_all_budgets(step_count: int) -> bool:
        quantity = Decimal(shares_for_step(step_count))
        executed_notional = worst_entry_price * quantity
        buy_cash = _buy_cash_out(
            entry,
            quantity,
            commission,
            minimum,
            slippage,
            additional_fee,
        )
        stop_cash = _sell_cash_in(
            stop,
            quantity,
            commission,
            minimum,
            sell_tax,
            slippage,
            additional_fee,
        )
        return (
            buy_cash <= available_cash
            and buy_cash - stop_cash <= risk
            and executed_notional <= stage_cap
            and executed_notional <= board_cap
            and executed_notional <= overnight_cap
        )

    if not within_all_budgets(0):
        return 0
    lower = 0
    upper = maximum_steps
    while lower < upper:
        middle = (lower + upper + 1) // 2
        if within_all_budgets(middle):
            lower = middle
        else:
            upper = middle - 1
    return shares_for_step(lower)


def get_environment_policy(state: str) -> Dict[str, Optional[float]]:
    """Return a copy of the fixed exposure policy for a market state."""

    if not isinstance(state, str):
        raise TypeError("state must be a string")
    normalized = state.strip().lower()
    try:
        return dict(_ENVIRONMENT_POLICIES[normalized])
    except KeyError as exc:
        allowed = ", ".join(sorted(_ENVIRONMENT_POLICIES))
        raise ValueError(
            f"unknown environment state {state!r}; expected one of: {allowed}"
        ) from exc


def _normalized_choice(value: Any, name: str, allowed: set[str]) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise ValueError(
            f"unknown {name} {value!r}; expected one of: "
            + ", ".join(sorted(allowed))
        )
    return normalized


def classify_environment_state(
    *,
    index_state: str,
    index_turning_up: bool,
    sentiment_state: str,
    style_setup_match: str,
    mainline_state: str,
) -> str:
    """Derive the conservative environment state from the approved gates."""

    index = _normalized_choice(
        index_state,
        "index_state",
        {"rising", "high_range", "falling", "low_range", "unknown"},
    )
    turning_up = _as_bool(index_turning_up, "index_turning_up")
    sentiment = _normalized_choice(
        sentiment_state,
        "sentiment_state",
        {
            "icepoint",
            "repair",
            "main_rise",
            "climax",
            "strong_divergence",
            "retreat",
            "icepoint_repair",
            "unknown",
        },
    )
    style_match = _normalized_choice(
        style_setup_match,
        "style_setup_match",
        {"matched", "mixed", "mismatched", "unknown"},
    )
    mainline = _normalized_choice(
        mainline_state,
        "mainline_state",
        {"confirmed", "low_resonance", "mixed", "collapsed", "unknown"},
    )

    if sentiment in {"icepoint", "retreat"} or mainline == "collapsed":
        return "defense"
    if index == "falling":
        if (
            sentiment == "icepoint_repair"
            and mainline == "low_resonance"
            and style_match == "matched"
        ):
            return "ice_trial"
        return "defense"
    if (
        index == "unknown"
        or sentiment == "unknown"
        or style_match in {"mismatched", "unknown"}
        or mainline in {"low_resonance", "mixed", "unknown"}
    ):
        return "insufficient"
    if (
        (index == "rising" or (index == "low_range" and turning_up))
        and sentiment in {"repair", "main_rise"}
        and mainline == "confirmed"
        and style_match == "matched"
    ):
        return "attack"
    if mainline == "confirmed" and style_match in {"matched", "mixed"}:
        return "cautious"
    return "insufficient"


def _calculate_overnight_notional_cap_decimal(
    *,
    equity: Any,
    limit_down_fraction: Any,
    tier: str,
    existing_single_stress_fraction: Any,
    existing_cluster_stress_fraction: Any,
) -> Decimal:
    """Return the exact Decimal overnight cap used by the trading endpoint."""

    account_equity = _non_negative_money(equity, "equity")
    limit_down = _non_negative_fraction(
        limit_down_fraction,
        "limit_down_fraction",
    )
    if limit_down == _ZERO:
        raise ValueError("limit_down_fraction must be greater than zero")
    existing_single_stress = _non_negative_fraction(
        existing_single_stress_fraction,
        "existing_single_stress_fraction",
    )
    existing_cluster_stress = _non_negative_fraction(
        existing_cluster_stress_fraction,
        "existing_cluster_stress_fraction",
    )
    tier_config = get_risk_tier_config(tier)
    single_stress = Decimal(str(tier_config["single_gap_stress"]))
    for name, fraction in (
        ("limit_down_fraction", limit_down),
        ("existing_single_stress_fraction", existing_single_stress),
        ("existing_cluster_stress_fraction", existing_cluster_stress),
    ):
        if fraction > _ONE:
            raise ValueError(f"{name} must not exceed 1")
    if existing_single_stress > existing_cluster_stress:
        raise ValueError(
            "existing single-symbol stress must not exceed existing cluster stress"
        )

    remaining_single_stress = max(_ZERO, single_stress - existing_single_stress)
    remaining_cluster_stress = max(
        _ZERO,
        _MAX_CLUSTER_GAP_STRESS - existing_cluster_stress,
    )
    allowed_stress = min(remaining_single_stress, remaining_cluster_stress)
    return account_equity * allowed_stress / limit_down


@_uses_local_decimal_context
def calculate_overnight_notional_cap(
    *,
    equity: Any,
    limit_down_fraction: Any,
    tier: str,
    existing_single_stress_fraction: Any,
    existing_cluster_stress_fraction: Any,
) -> float:
    """Cap overnight notional by fixed single-name and cluster stress limits."""

    cap = _calculate_overnight_notional_cap_decimal(
        equity=equity,
        limit_down_fraction=limit_down_fraction,
        tier=tier,
        existing_single_stress_fraction=existing_single_stress_fraction,
        existing_cluster_stress_fraction=existing_cluster_stress_fraction,
    )
    return _finite_float(cap, "overnight_notional_cap")


def get_risk_tier_config(tier: str) -> Dict[str, float]:
    """Return a copy of the approved limits for a risk tier."""

    if not isinstance(tier, str):
        raise TypeError("tier must be a string")
    normalized = tier.strip().lower()
    try:
        return dict(_RISK_TIER_CONFIGS[normalized])
    except KeyError as exc:
        allowed = ", ".join(sorted(_RISK_TIER_CONFIGS))
        raise ValueError(f"unknown risk tier {tier!r}; expected one of: {allowed}") from exc


@_uses_local_decimal_context
def evaluate_setup_tier(
    history: Sequence,
    setup: str,
    *,
    history_cutoff_date: Any,
    discipline_rate: Optional[Any] = None,
    max_drawdown_fraction: Optional[Any] = None,
) -> Dict[str, Any]:
    """Evaluate one setup from closed, cost-complete, auditable trade rows.

    Per-trade ``followed_plan`` and setup-equity values are authoritative.
    Optional aggregate values are accepted only as cross-checks and cannot
    override the trade log.
    """

    if isinstance(history, (str, bytes)) or not isinstance(history, Sequence):
        raise TypeError("history must be a sequence of trade mappings")
    approved_setups = {"low_first_board", "mainline_pullback"}
    if not isinstance(setup, str) or not setup.strip():
        raise ValueError("setup must be a non-empty string")
    normalized_setup = setup.strip().lower()
    if normalized_setup not in approved_setups:
        raise ValueError(
            "setup must be an approved setup: low_first_board or mainline_pullback"
        )
    cutoff_date = _iso_date(history_cutoff_date, "history_cutoff_date")

    matching_net_r = []
    matching_discipline = []
    complete_discipline_records = True
    complete_equity_records = True
    seen_trade_ids = set()
    previous_exit_date = None
    previous_exit_timestamp = None
    previous_equity_after = None
    equity_points = []
    for index, trade in enumerate(history):
        if not isinstance(trade, Mapping):
            raise TypeError(f"history[{index}] must be a mapping")
        if "setup" not in trade:
            raise ValueError(f"history[{index}] is missing setup")
        trade_setup = trade["setup"]
        if not isinstance(trade_setup, str) or trade_setup not in approved_setups:
            raise ValueError(f"history[{index}].setup is not an approved setup")
        if trade_setup != normalized_setup:
            continue
        trade_id = trade.get("trade_id")
        if not isinstance(trade_id, str) or not trade_id.strip():
            raise ValueError(f"history[{index}].trade_id must be a non-empty string")
        if trade_id in seen_trade_ids:
            raise ValueError(f"history[{index}].trade_id is duplicated")
        seen_trade_ids.add(trade_id)

        parsed_exit_date = _iso_date(
            trade.get("exit_date"),
            f"history[{index}].exit_date",
        )
        if parsed_exit_date > cutoff_date:
            raise ValueError(
                f"history[{index}].exit_date is after history_cutoff_date"
            )
        if previous_exit_date is not None and parsed_exit_date < previous_exit_date:
            raise ValueError(
                f"history[{index}] is not in exit_date order for {normalized_setup}"
            )
        previous_exit_date = parsed_exit_date
        parsed_exit_timestamp = _iso_shanghai_datetime(
            trade.get("exit_timestamp"),
            f"history[{index}].exit_timestamp",
        )
        if parsed_exit_timestamp.date() != parsed_exit_date:
            raise ValueError(
                f"history[{index}].exit_timestamp date does not match exit_date"
            )
        if parsed_exit_date.weekday() >= 5:
            raise ValueError(
                f"history[{index}].exit_date is a weekend, not a trading session"
            )
        session_confirmed = _as_bool(
            trade.get("trading_session_confirmed"),
            f"history[{index}].trading_session_confirmed",
        )
        if not session_confirmed:
            raise ValueError(
                f"history[{index}].trading_session_confirmed must be true"
            )
        session_evidence_id = trade.get("trading_session_evidence_id")
        if not isinstance(session_evidence_id, str) or not session_evidence_id.strip():
            raise ValueError(
                f"history[{index}].trading_session_evidence_id must be a "
                "non-empty string"
            )
        exit_clock = parsed_exit_timestamp.time()
        opening_match = time(9, 25) <= exit_clock < time(9, 26)
        morning_session = time(9, 30) <= exit_clock <= time(11, 30)
        afternoon_session = time(13, 0) <= exit_clock <= time(15, 0)
        if not (opening_match or morning_session or afternoon_session):
            raise ValueError(
                f"history[{index}].exit_timestamp is outside the supported "
                "ordinary A-share trading session"
            )
        if (
            previous_exit_timestamp is not None
            and parsed_exit_timestamp <= previous_exit_timestamp
        ):
            raise ValueError(
                f"history[{index}].exit_timestamp is not in strict order "
                f"for {normalized_setup}"
            )
        previous_exit_timestamp = parsed_exit_timestamp

        if trade.get("closed") is not True:
            raise ValueError(f"history[{index}].closed must be true")
        if trade.get("costs_included") is not True:
            raise ValueError(f"history[{index}].costs_included must be true")

        symbol = trade.get("symbol")
        exchange = trade.get("exchange")
        try:
            _derive_market_segment(symbol, exchange)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"history[{index}] symbol/exchange is outside the bundled "
                "ordinary A-share scope"
            ) from exc

        for field in ("exit_reason", "evidence_log_id"):
            value = trade.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"history[{index}].{field} must be a non-empty string")
        entry_price = _positive_decimal(
            trade.get("entry_price"),
            f"history[{index}].entry_price",
        )
        exit_price = _positive_decimal(
            trade.get("exit_price"),
            f"history[{index}].exit_price",
        )
        planned_entry_price = _price_on_tick(
            trade.get("planned_entry_price"),
            f"history[{index}].planned_entry_price",
        )
        planned_stop_price = _price_on_tick(
            trade.get("planned_stop_price"),
            f"history[{index}].planned_stop_price",
        )
        planned_exit_price = _positive_decimal(
            trade.get("planned_exit_price"),
            f"history[{index}].planned_exit_price",
        )
        shares = _positive_int(trade.get("shares"), f"history[{index}].shares")
        gross_pnl = _as_decimal(
            trade.get("gross_pnl_cash"),
            f"history[{index}].gross_pnl_cash",
        )
        commission_cash = _non_negative_decimal(
            trade.get("commission_cash"),
            f"history[{index}].commission_cash",
        )
        tax_cash = _non_negative_decimal(
            trade.get("tax_cash"),
            f"history[{index}].tax_cash",
        )
        additional_fees_cash = _non_negative_decimal(
            trade.get("additional_fees_cash"),
            f"history[{index}].additional_fees_cash",
        )
        slippage_cash = _as_decimal(
            trade.get("slippage_cash"),
            f"history[{index}].slippage_cash",
        )
        net_pnl = _as_decimal(
            trade.get("net_pnl"),
            f"history[{index}].net_pnl",
        )
        planned_risk_cash = _positive_decimal(
            trade.get("planned_risk_cash"),
            f"history[{index}].planned_risk_cash",
        )
        net_r = _as_decimal(trade.get("net_r"), f"history[{index}].net_r")
        planned_slippage_rate = _non_negative_fraction(
            trade.get("planned_slippage_rate_per_side"),
            f"history[{index}].planned_slippage_rate_per_side",
        )
        if planned_slippage_rate < _MINIMUM_SLIPPAGE_RATE:
            raise ValueError(
                f"history[{index}].planned_slippage_rate_per_side is below "
                "the conservative exact-plan floor"
            )
        if planned_slippage_rate > _MAXIMUM_PLANNED_SLIPPAGE_RATE:
            raise ValueError(
                f"history[{index}].planned_slippage_rate_per_side must not "
                "exceed 5% for an in-scope setup"
            )
        if not planned_stop_price < planned_entry_price:
            raise ValueError(
                f"history[{index}] planned prices must satisfy "
                "planned_stop_price < planned_entry_price"
            )
        if (
            (planned_entry_price - planned_stop_price) / planned_entry_price
            > _MAX_STRUCTURAL_STOP
        ):
            raise ValueError(
                f"history[{index}] planned structural stop distance exceeds 5%"
            )

        (
            historical_commission_rate,
            historical_minimum_commission,
            historical_sell_tax_rate,
            _,
            historical_additional_fee_rate,
        ) = _validated_cost_parameters(
            trade.get("commission_rate"),
            trade.get("minimum_commission"),
            trade.get("sell_tax_rate"),
            0,
            trade.get("additional_fee_rate_per_side"),
            trade.get("commission_basis"),
        )
        _require_realistic_exact_plan_costs(
            commission=historical_commission_rate,
            minimum=historical_minimum_commission,
            sell_tax=historical_sell_tax_rate,
            slippage=_MINIMUM_SLIPPAGE_RATE,
            additional_fee=historical_additional_fee_rate,
        )

        quantity = Decimal(shares)
        entry_notional = entry_price * quantity
        exit_notional = exit_price * quantity
        expected_commission_cash = _charged_commission(
            entry_notional,
            historical_commission_rate,
            historical_minimum_commission,
        ) + _charged_commission(
            exit_notional,
            historical_commission_rate,
            historical_minimum_commission,
        )
        expected_tax_cash = exit_notional * historical_sell_tax_rate
        expected_additional_fees_cash = (
            entry_notional + exit_notional
        ) * historical_additional_fee_rate
        for field, actual, expected in (
            ("commission_cash", commission_cash, expected_commission_cash),
            ("tax_cash", tax_cash, expected_tax_cash),
            (
                "additional_fees_cash",
                additional_fees_cash,
                expected_additional_fees_cash,
            ),
        ):
            if abs(actual - expected) > _CASH_RECONCILIATION_TOLERANCE:
                raise ValueError(
                    f"history[{index}].{field} does not reconcile to fills "
                    "and the recorded fee configuration"
                )

        expected_planned_risk_cash = _buy_cash_out(
            planned_entry_price,
            quantity,
            historical_commission_rate,
            historical_minimum_commission,
            planned_slippage_rate,
            historical_additional_fee_rate,
        ) - _sell_cash_in(
            planned_stop_price,
            quantity,
            historical_commission_rate,
            historical_minimum_commission,
            historical_sell_tax_rate,
            planned_slippage_rate,
            historical_additional_fee_rate,
        )
        minimum_friction_planned_risk_cash = _buy_cash_out(
            planned_entry_price,
            quantity,
            _MINIMUM_ALL_IN_FEE_RATE,
            _MINIMUM_COMMISSION_PER_SIDE,
            _MINIMUM_SLIPPAGE_RATE,
            _ZERO,
        ) - _sell_cash_in(
            planned_stop_price,
            quantity,
            _MINIMUM_ALL_IN_FEE_RATE,
            _MINIMUM_COMMISSION_PER_SIDE,
            _MINIMUM_SELL_TAX_RATE,
            _MINIMUM_SLIPPAGE_RATE,
            _ZERO,
        )
        if expected_planned_risk_cash <= _ZERO:
            raise ValueError(
                f"history[{index}].planned_risk_cash must be positive after costs"
            )
        if (
            abs(planned_risk_cash - expected_planned_risk_cash)
            > _CASH_RECONCILIATION_TOLERANCE
        ):
            raise ValueError(
                f"history[{index}].planned_risk_cash does not reconcile to "
                "planned entry, planned stop, shares, slippage, and fees"
            )

        minimum_sell_tax = (
            exit_price * quantity * _MINIMUM_SELL_TAX_RATE
        )
        if (
            commission_cash + _CASH_RECONCILIATION_TOLERANCE
            < _MINIMUM_ROUND_TRIP_COMMISSION
        ):
            raise ValueError(
                f"history[{index}].commission_cash is below the in-scope "
                "round-trip commission floor"
            )
        if tax_cash + _CASH_RECONCILIATION_TOLERANCE < minimum_sell_tax:
            raise ValueError(
                f"history[{index}].tax_cash is below the in-scope A-share "
                "rule floor"
            )

        expected_gross_pnl = (exit_price - entry_price) * quantity
        if abs(gross_pnl - expected_gross_pnl) > _CASH_RECONCILIATION_TOLERANCE:
            raise ValueError(
                f"history[{index}].gross_pnl_cash does not reconcile to fills"
            )
        expected_slippage_cash = (
            (entry_price - planned_entry_price)
            + (planned_exit_price - exit_price)
        ) * quantity
        if (
            abs(slippage_cash - expected_slippage_cash)
            > _CASH_RECONCILIATION_TOLERANCE
        ):
            raise ValueError(
                f"history[{index}].slippage_cash does not reconcile to planned "
                "and fill prices"
            )
        reported_components_net_pnl = (
            gross_pnl - commission_cash - tax_cash - additional_fees_cash
        )
        if (
            abs(net_pnl - reported_components_net_pnl)
            > _CASH_RECONCILIATION_TOLERANCE
        ):
            raise ValueError(
                f"history[{index}].net_pnl does not reconcile to cost details"
            )
        fill_and_fee_net_pnl = (
            expected_gross_pnl
            - expected_commission_cash
            - expected_tax_cash
            - expected_additional_fees_cash
        )
        reported_net_r = net_pnl / planned_risk_cash
        if abs(net_r - reported_net_r) > _R_RECONCILIATION_TOLERANCE:
            raise ValueError(
                f"history[{index}].net_r does not reconcile to net_pnl and "
                "planned_risk_cash"
            )
        net_pnl_sign = _decimal_sign(net_pnl)
        fill_and_fee_sign = _decimal_sign(fill_and_fee_net_pnl)
        if net_pnl_sign != fill_and_fee_sign:
            raise ValueError(
                f"history[{index}].net_pnl sign conflicts with the "
                "cost-recomputed fills"
            )
        conservative_net_pnl_without_equity = min(
            net_pnl,
            _ZERO if fill_and_fee_sign == 0 else fill_and_fee_net_pnl,
        )
        if "followed_plan" in trade:
            followed_plan = trade["followed_plan"]
            if not isinstance(followed_plan, bool):
                raise TypeError(f"history[{index}].followed_plan must be bool")
            matching_discipline.append(followed_plan)
        else:
            complete_discipline_records = False

        if "setup_equity_before" not in trade or "setup_equity_after" not in trade:
            complete_equity_records = False
            previous_equity_after = None
            matching_net_r.append(
                conservative_net_pnl_without_equity
                / (
                    max(
                        expected_planned_risk_cash,
                        minimum_friction_planned_risk_cash,
                    )
                    if conservative_net_pnl_without_equity >= _ZERO
                    else min(
                        expected_planned_risk_cash,
                        minimum_friction_planned_risk_cash,
                    )
                )
            )
            continue
        equity_before = _positive_decimal(
            trade["setup_equity_before"],
            f"history[{index}].setup_equity_before",
        )
        equity_after = _positive_decimal(
            trade["setup_equity_after"],
            f"history[{index}].setup_equity_after",
        )
        equity_delta = equity_after - equity_before
        if (
            abs(equity_delta - net_pnl)
            > _CASH_RECONCILIATION_TOLERANCE
        ):
            raise ValueError(
                f"history[{index}].net_pnl does not reconcile to the setup equity curve"
            )
        equity_delta_sign = _decimal_sign(equity_delta)
        if net_pnl_sign != equity_delta_sign:
            raise ValueError(
                f"history[{index}].net_pnl sign conflicts with the setup equity curve"
            )
        conservative_net_pnl = min(
            conservative_net_pnl_without_equity,
            equity_delta,
        )
        conservative_risk_denominator = (
            max(
                expected_planned_risk_cash,
                minimum_friction_planned_risk_cash,
            )
            if conservative_net_pnl >= _ZERO
            else min(
                expected_planned_risk_cash,
                minimum_friction_planned_risk_cash,
            )
        )
        matching_net_r.append(conservative_net_pnl / conservative_risk_denominator)
        if previous_equity_after is not None and equity_before != previous_equity_after:
            raise ValueError(
                f"history[{index}].setup_equity_before conflicts with the prior "
                "setup_equity_after"
            )
        previous_equity_after = equity_after
        equity_points.append((equity_before, equity_after))

    setup_trade_count = len(matching_net_r)
    expectancy = (
        sum(matching_net_r, _ZERO) / Decimal(setup_trade_count)
        if setup_trade_count
        else _ZERO
    )

    if not matching_net_r or not complete_discipline_records:
        observed_discipline = None
    else:
        observed_discipline = (
            Decimal(sum(matching_discipline)) / Decimal(len(matching_discipline))
        )

    drawdown = None
    if matching_net_r and complete_equity_records and len(equity_points) == len(matching_net_r):
        peak = equity_points[0][0]
        maximum_drawdown = _ZERO
        for equity_before, equity_after in equity_points:
            peak = max(peak, equity_before)
            maximum_drawdown = max(
                maximum_drawdown,
                (peak - equity_after) / peak,
            )
            peak = max(peak, equity_after)
        drawdown = maximum_drawdown

    if discipline_rate is not None:
        supplied_discipline = _non_negative_decimal(
            discipline_rate,
            "discipline_rate",
        )
        if supplied_discipline > _ONE:
            raise ValueError("discipline_rate must not exceed 1")
        if (
            observed_discipline is not None
            and supplied_discipline != observed_discipline
        ):
            raise ValueError(
                "discipline_rate conflicts with the observed followed_plan history"
            )
    if max_drawdown_fraction is not None:
        supplied_drawdown = _non_negative_decimal(
            max_drawdown_fraction,
            "max_drawdown_fraction",
        )
        if supplied_drawdown > _ONE:
            raise ValueError("max_drawdown_fraction must not exceed 1")
        if drawdown is not None and supplied_drawdown != drawdown:
            raise ValueError(
                "max_drawdown_fraction conflicts with the observed setup equity curve"
            )
    if observed_discipline is not None and observed_discipline > _ONE:
        raise ValueError("discipline_rate must not exceed 1")
    upgrade_eligible = (
        setup_trade_count >= _MIN_UPGRADE_TRADES
        and expectancy > _ZERO
        and observed_discipline is not None
        and observed_discipline >= _MIN_UPGRADE_DISCIPLINE
        and drawdown is not None
        and drawdown <= _MAX_UPGRADE_DRAWDOWN
    )
    tier = "upgrade" if upgrade_eligible else "validation"
    tier_config = get_risk_tier_config(tier)
    return {
        "tier": tier,
        "risk_fraction": tier_config["single_risk"],
        "setup_trade_count": setup_trade_count,
        "net_expectancy_r": _finite_float(expectancy, "net_expectancy_r"),
        "discipline_rate": (
            None
            if observed_discipline is None
            else _finite_float(observed_discipline, "discipline_rate")
        ),
        "max_drawdown_fraction": (
            None
            if drawdown is None
            else _finite_float(drawdown, "max_drawdown_fraction")
        ),
    }


@_uses_local_decimal_context
def daily_lock_triggered(
    *,
    daily_loss_fraction: Any,
    stop_loss_count: int,
) -> bool:
    """Return whether new positions must be locked for the rest of the day."""

    loss = _non_negative_fraction(daily_loss_fraction, "daily_loss_fraction")
    stops = _non_negative_int(stop_loss_count, "stop_loss_count")
    return loss >= Decimal("0.015") or stops >= 2


@_uses_local_decimal_context
def weekly_loss_action(
    *,
    weekly_loss_fraction: Any,
    lock_remainder_of_week: bool = False,
    reduced_risk_trading_days_remaining: int = 0,
) -> Dict[str, Any]:
    """Apply the persisted weekly lock and following five-day recovery state."""

    loss = _non_negative_fraction(weekly_loss_fraction, "weekly_loss_fraction")
    persisted_lock = _as_bool(lock_remainder_of_week, "lock_remainder_of_week")
    remaining_days = _non_negative_int(
        reduced_risk_trading_days_remaining,
        "reduced_risk_trading_days_remaining",
    )
    if remaining_days > 5:
        raise ValueError("reduced_risk_trading_days_remaining must not exceed 5")
    triggered = loss >= Decimal("0.05")
    active_lock = triggered or persisted_lock
    if triggered:
        remaining_days = 5
    elif persisted_lock and remaining_days != 5:
        raise ValueError(
            "a persisted weekly lock requires remaining recovery days to stay at 5"
        )
    current_multiplier = 0.0 if active_lock else (0.5 if remaining_days else 1.0)
    return {
        "lock_remainder_of_week": active_lock,
        "reduced_risk_trading_days": remaining_days,
        "risk_multiplier": current_multiplier,
        "post_lock_risk_multiplier": 0.5 if remaining_days else 1.0,
    }


@_uses_local_decimal_context
def evaluate_new_position_plan(
    *,
    equity: Any,
    cash: Any,
    open_order_exposure: Any,
    decision_date: Any,
    decision_time: Any,
    data_cutoff: Any,
    latest_completed_trading_date: Any,
    rule_version_checked_at: Any,
    rules_match_bundled_configuration: bool,
    trading_session_confirmed: bool,
    final_auction_data_confirmed: bool,
    final_auction_price: Any,
    forbidden_chase_price: Any,
    symbol: str,
    exchange: str,
    entry_price: Any,
    stop_price: Any,
    target_price: Any,
    index_state: str,
    index_turning_up: bool,
    sentiment_state: str,
    style_setup_match: str,
    mainline_state: str,
    setup: str,
    trade_history: Sequence,
    market_segment: str,
    existing_portfolio_risk: Any,
    existing_total_notional: Any,
    existing_symbol_notional: Any,
    existing_symbol_stress_fraction: Any,
    sector_notional_cap: Any,
    limit_down_fraction: Any,
    existing_cluster_stress_fraction: Any,
    existing_ice_trial_plan_count: int,
    daily_loss_fraction: Any,
    stop_loss_count: int,
    weekly_loss_fraction: Any,
    weekly_lock_remainder_of_week: bool,
    weekly_recovery_days_remaining: int,
    commission_rate: Any,
    minimum_commission: Any,
    sell_tax_rate: Any,
    slippage_rate_per_side: Any,
    additional_fee_rate_per_side: Any,
    commission_basis: Any,
) -> Dict[str, Any]:
    """Atomically apply every fixed gate before returning a share quantity."""

    account_equity = _positive_money(equity, "equity")
    reported_cash = _non_negative_money(cash, "cash")
    pending_cash = _non_negative_money(
        open_order_exposure,
        "open_order_exposure",
    )
    plan_date = _iso_date(decision_date, "decision_date")
    if plan_date.weekday() >= 5:
        raise ValueError("decision_date is a weekend, not a trading session")
    session_attested = _as_bool(
        trading_session_confirmed,
        "trading_session_confirmed",
    )
    if not session_attested:
        raise ValueError(
            "trading_session_confirmed must be true after checking an official "
            "trading calendar and temporary closure notices"
        )
    decision_timestamp = _iso_shanghai_datetime(decision_time, "decision_time")
    cutoff_timestamp = _iso_shanghai_datetime(data_cutoff, "data_cutoff")
    if decision_timestamp.date() != plan_date:
        raise ValueError("decision_time date must match decision_date")
    if cutoff_timestamp > decision_timestamp:
        raise ValueError("data_cutoff must not be later than decision_time")
    decision_clock = decision_timestamp.time()
    if decision_clock < time(9, 25) or decision_clock >= time(9, 30):
        raise ValueError(
            "an exact pre-open plan requires final auction data between "
            "09:25:00 and 09:29:59 Asia/Shanghai"
        )
    if cutoff_timestamp.date() != plan_date or cutoff_timestamp.time() < time(9, 25):
        raise ValueError(
            "data_cutoff for an exact post-auction plan must be on decision_date "
            "at or after 09:25 Asia/Shanghai"
        )
    final_auction_confirmed = _as_bool(
        final_auction_data_confirmed,
        "final_auction_data_confirmed",
    )
    if not final_auction_confirmed:
        raise ValueError(
            "final_auction_data_confirmed must be true for an exact pre-open plan"
        )
    completed_date = _iso_date(
        latest_completed_trading_date,
        "latest_completed_trading_date",
    )
    if completed_date >= plan_date:
        raise ValueError(
            "latest_completed_trading_date must be earlier than decision_date"
        )
    if plan_date < _BUNDLED_RULES_AS_OF:
        raise ValueError(
            "decision_date predates the bundled rule snapshot dated "
            f"{_BUNDLED_RULES_AS_OF.isoformat()}"
        )
    rule_checked_date = _iso_date(
        rule_version_checked_at,
        "rule_version_checked_at",
    )
    if rule_checked_date != plan_date:
        raise ValueError(
            "rule_version_checked_at must equal decision_date for an exact plan"
        )
    rules_attested = _as_bool(
        rules_match_bundled_configuration,
        "rules_match_bundled_configuration",
    )
    if not rules_attested:
        raise ValueError(
            "rules_match_bundled_configuration must be true after a fresh "
            "official-rule check"
        )
    available_cash = max(_ZERO, reported_cash - pending_cash)
    entry = _price_on_tick(entry_price, "entry_price")
    stop = _price_on_tick(stop_price, "stop_price")
    target = _price_on_tick(target_price, "target_price")
    auction_price = _price_on_tick(final_auction_price, "final_auction_price")
    chase_price = _price_on_tick(forbidden_chase_price, "forbidden_chase_price")
    if entry > chase_price:
        raise ValueError("entry_price must not exceed forbidden_chase_price")
    effective_entry = max(entry, auction_price)
    (
        canonical_symbol,
        normalized_exchange,
        derived_segment,
    ) = _derive_market_segment(symbol, exchange)
    normalized_segment = _normalized_choice(
        market_segment,
        "market_segment",
        set(_MARKET_SEGMENT_RULES),
    )
    if normalized_segment != derived_segment:
        raise ValueError(
            "market_segment does not match the segment derived from symbol"
        )
    try:
        segment_rules = _MARKET_SEGMENT_RULES[normalized_segment]
    except KeyError as exc:
        raise ValueError(
            "market_segment must be 'main_board', 'chinext', or 'star'"
        ) from exc
    board_cap_key = segment_rules["board_cap_key"]
    minimum_shares = segment_rules["minimum_order_shares"]
    increment = segment_rules["share_increment"]
    required_limit_down = segment_rules["limit_down_fraction"]
    supplied_limit_down = _non_negative_fraction(
        limit_down_fraction,
        "limit_down_fraction",
    )
    if supplied_limit_down == _ZERO:
        raise ValueError("limit_down_fraction must be greater than zero")
    if supplied_limit_down != required_limit_down:
        raise ValueError(
            "limit_down_fraction does not match the in-scope "
            f"{normalized_segment} rule verified as of 2026-09-01"
        )
    portfolio_risk = _non_negative_money(
        existing_portfolio_risk,
        "existing_portfolio_risk",
    )
    total_notional = _non_negative_money(
        existing_total_notional,
        "existing_total_notional",
    )
    symbol_notional = _non_negative_money(
        existing_symbol_notional,
        "existing_symbol_notional",
    )
    symbol_stress = _non_negative_fraction(
        existing_symbol_stress_fraction,
        "existing_symbol_stress_fraction",
    )
    cluster_stress = _non_negative_fraction(
        existing_cluster_stress_fraction,
        "existing_cluster_stress_fraction",
    )
    ice_trial_plan_count = _non_negative_int(
        existing_ice_trial_plan_count,
        "existing_ice_trial_plan_count",
    )
    sector_cap = _non_negative_money(
        sector_notional_cap,
        "sector_notional_cap",
    )
    (
        commission,
        minimum,
        sell_tax,
        slippage,
        additional_fee,
    ) = _validated_cost_parameters(
        commission_rate,
        minimum_commission,
        sell_tax_rate,
        slippage_rate_per_side,
        additional_fee_rate_per_side,
        commission_basis,
    )
    _require_realistic_exact_plan_costs(
        commission=commission,
        minimum=minimum,
        sell_tax=sell_tax,
        slippage=slippage,
        additional_fee=additional_fee,
    )
    if not stop < entry < target:
        raise ValueError("prices must satisfy stop_price < entry_price < target_price")
    if portfolio_risk > account_equity:
        raise ValueError("existing_portfolio_risk must not exceed equity")
    if symbol_notional > total_notional:
        raise ValueError(
            "existing_symbol_notional must not exceed existing_total_notional"
        )
    if symbol_stress > cluster_stress:
        raise ValueError(
            "existing symbol stress must not exceed existing cluster stress"
        )
    expected_symbol_stress = (
        symbol_notional * required_limit_down / account_equity
    )
    if (
        abs(symbol_stress - expected_symbol_stress)
        > _STRESS_RECONCILIATION_TOLERANCE
    ):
        raise ValueError(
            "existing symbol stress does not reconcile to symbol notional, "
            "equity, and segment limit-down stress"
        )

    environment_state = classify_environment_state(
        index_state=index_state,
        index_turning_up=index_turning_up,
        sentiment_state=sentiment_state,
        style_setup_match=style_setup_match,
        mainline_state=mainline_state,
    )
    environment = (
        {"total_cap": 0.0, "minimum_net_r": None}
        if environment_state == "insufficient"
        else get_environment_policy(environment_state)
    )
    setup_evaluation = evaluate_setup_tier(
        trade_history,
        setup,
        history_cutoff_date=completed_date.isoformat(),
    )
    risk_tier = setup_evaluation["tier"]
    tier_config = get_risk_tier_config(risk_tier)

    daily_locked = daily_lock_triggered(
        daily_loss_fraction=daily_loss_fraction,
        stop_loss_count=stop_loss_count,
    )
    weekly_action = weekly_loss_action(
        weekly_loss_fraction=weekly_loss_fraction,
        lock_remainder_of_week=weekly_lock_remainder_of_week,
        reduced_risk_trading_days_remaining=weekly_recovery_days_remaining,
    )
    minimum_net_r = environment["minimum_net_r"]

    def no_trade(reason_code: str, **details: Any) -> Dict[str, Any]:
        result = {
            "decision": "no_trade",
            "reason_codes": [reason_code],
            "recommended_shares": 0,
            "position_notional": 0.0,
            "risk_budget": 0.0,
            "net_reward_risk": None,
            "minimum_net_r": minimum_net_r,
            "environment_state": environment_state,
            "risk_tier": risk_tier,
            "symbol": canonical_symbol,
            "exchange": normalized_exchange.upper(),
            "decision_date": plan_date.isoformat(),
            "decision_time": decision_timestamp.isoformat(),
            "data_cutoff": cutoff_timestamp.isoformat(),
            "latest_completed_trading_date": completed_date.isoformat(),
            "rule_version_checked_at": rule_checked_date.isoformat(),
            "bundled_rules_as_of": _BUNDLED_RULES_AS_OF.isoformat(),
            "rules_match_bundled_configuration": rules_attested,
            "rule_attestation_independently_verified": False,
            "trading_session_confirmed": session_attested,
            "trading_session_independently_verified": False,
            "final_auction_data_confirmed": final_auction_confirmed,
            "final_auction_price": _finite_float(
                auction_price,
                "final_auction_price",
            ),
            "forbidden_chase_price": _finite_float(
                chase_price,
                "forbidden_chase_price",
            ),
            "planned_entry_price": _finite_float(
                entry,
                "planned_entry_price",
            ),
            "effective_entry_price": _finite_float(
                effective_entry,
                "effective_entry_price",
            ),
            "market_segment": normalized_segment,
            "limit_down_fraction": _finite_float(
                required_limit_down,
                "limit_down_fraction",
            ),
            "minimum_order_shares": minimum_shares,
            "share_increment": increment,
            "setup_evaluation": setup_evaluation,
            "weekly_recovery_days_remaining": weekly_action[
                "reduced_risk_trading_days"
            ],
        }
        result.update(details)
        return result

    if daily_locked:
        return no_trade("DAILY_NEW_POSITION_LOCK")
    if weekly_action["lock_remainder_of_week"]:
        return no_trade("WEEKLY_NEW_POSITION_LOCK")
    if pending_cash > _ZERO:
        return no_trade("OPEN_ORDER_EXPOSURE_UNRESOLVED")
    if environment_state == "insufficient":
        return no_trade("ENVIRONMENT_INSUFFICIENT")
    if environment["total_cap"] == 0.0 or minimum_net_r is None:
        return no_trade("ENVIRONMENT_DEFENSE")
    if auction_price > chase_price:
        return no_trade("FINAL_AUCTION_ABOVE_CHASE_LIMIT")
    if auction_price <= stop:
        return no_trade("FINAL_AUCTION_INVALIDATES_PRICE_STRUCTURE")
    if not stop < effective_entry < target:
        return no_trade("FINAL_AUCTION_INVALIDATES_PRICE_STRUCTURE")
    if (effective_entry - stop) / effective_entry > _MAX_STRUCTURAL_STOP:
        return no_trade("STOP_TOO_WIDE")
    if environment_state == "ice_trial" and ice_trial_plan_count > 0:
        return no_trade("ICE_TRIAL_PLAN_SLOT_OCCUPIED")

    risk_multiplier = Decimal(str(weekly_action["risk_multiplier"]))
    single_risk_budget = (
        account_equity
        * Decimal(str(tier_config["single_risk"]))
        * risk_multiplier
    )
    portfolio_risk_cap = (
        account_equity
        * Decimal(str(tier_config["portfolio_risk"]))
        * risk_multiplier
    )
    remaining_portfolio_risk = max(_ZERO, portfolio_risk_cap - portfolio_risk)
    risk_budget = min(single_risk_budget, remaining_portfolio_risk)

    total_cap_fraction = min(
        Decimal(str(environment["total_cap"])),
        Decimal(str(tier_config["total_cap"])),
    )
    stage_notional_cap = max(
        _ZERO,
        account_equity * total_cap_fraction - total_notional,
    )
    board_notional_cap = max(
        _ZERO,
        account_equity * Decimal(str(tier_config[board_cap_key]))
        - symbol_notional,
    )
    board_and_sector_notional_cap = min(board_notional_cap, sector_cap)
    overnight_notional_cap = _calculate_overnight_notional_cap_decimal(
        equity=account_equity,
        limit_down_fraction=required_limit_down,
        tier=risk_tier,
        existing_single_stress_fraction=symbol_stress,
        existing_cluster_stress_fraction=cluster_stress,
    )

    shares = calculate_position_shares(
        cash=available_cash,
        entry_price=effective_entry,
        stop_price=stop,
        risk_budget=risk_budget,
        stage_notional_cap=stage_notional_cap,
        board_notional_cap=board_and_sector_notional_cap,
        overnight_notional_cap=overnight_notional_cap,
        minimum_order_shares=minimum_shares,
        share_increment=increment,
        commission_rate=commission,
        minimum_commission=minimum,
        sell_tax_rate=sell_tax,
        slippage_rate_per_side=slippage,
        additional_fee_rate_per_side=additional_fee,
        commission_basis=commission_basis,
    )
    risk_budget_float = _finite_float(risk_budget, "risk_budget")
    if shares == 0:
        return no_trade(
            "ONE_LOT_EXCEEDS_CAPS",
            risk_budget=risk_budget_float,
        )

    net_reward_risk_decimal = _calculate_net_reward_risk_decimal(
        entry=effective_entry,
        stop=stop,
        target=target,
        quantity=Decimal(shares),
        commission=commission,
        minimum=minimum,
        sell_tax=sell_tax,
        slippage=slippage,
        additional_fee=additional_fee,
    )
    net_reward_risk = _finite_float(
        net_reward_risk_decimal,
        "net_reward_risk",
    )
    position_notional = effective_entry * (_ONE + slippage) * Decimal(shares)
    if net_reward_risk_decimal < Decimal(str(minimum_net_r)):
        return no_trade(
            "NET_R_BELOW_THRESHOLD",
            risk_budget=risk_budget_float,
            net_reward_risk=net_reward_risk,
            candidate_position_notional=_finite_float(
                position_notional,
                "candidate_position_notional",
            ),
        )

    return {
        "decision": "trade",
        "reason_codes": [],
        "recommended_shares": shares,
        "position_notional": _finite_float(position_notional, "position_notional"),
        "risk_budget": risk_budget_float,
        "net_reward_risk": net_reward_risk,
        "minimum_net_r": minimum_net_r,
        "break_even_win_rate": calculate_break_even_win_rate(
            net_reward_risk=net_reward_risk
        ),
        "environment_state": environment_state,
        "risk_tier": risk_tier,
        "symbol": canonical_symbol,
        "exchange": normalized_exchange.upper(),
        "decision_date": plan_date.isoformat(),
        "decision_time": decision_timestamp.isoformat(),
        "data_cutoff": cutoff_timestamp.isoformat(),
        "latest_completed_trading_date": completed_date.isoformat(),
        "rule_version_checked_at": rule_checked_date.isoformat(),
        "bundled_rules_as_of": _BUNDLED_RULES_AS_OF.isoformat(),
        "rules_match_bundled_configuration": rules_attested,
        "rule_attestation_independently_verified": False,
        "trading_session_confirmed": session_attested,
        "trading_session_independently_verified": False,
        "final_auction_data_confirmed": final_auction_confirmed,
        "final_auction_price": _finite_float(
            auction_price,
            "final_auction_price",
        ),
        "forbidden_chase_price": _finite_float(
            chase_price,
            "forbidden_chase_price",
        ),
        "planned_entry_price": _finite_float(
            entry,
            "planned_entry_price",
        ),
        "effective_entry_price": _finite_float(
            effective_entry,
            "effective_entry_price",
        ),
        "market_segment": normalized_segment,
        "limit_down_fraction": _finite_float(
            required_limit_down,
            "limit_down_fraction",
        ),
        "minimum_order_shares": minimum_shares,
        "share_increment": increment,
        "setup_evaluation": setup_evaluation,
        "weekly_recovery_days_remaining": weekly_action[
            "reduced_risk_trading_days"
        ],
    }


def _parse_json_time(value: Any) -> time:
    if not isinstance(value, str):
        raise TypeError("current_time must be an ISO time string")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid current_time {value!r}; expected HH:MM[:SS]") from exc
    if parsed.utcoffset() is not None:
        raise ValueError(
            "current_time must not include a timezone; provide the Asia/Shanghai "
            "exchange clock"
        )
    return parsed


def dispatch_json_request(payload: Any) -> Any:
    """Dispatch one JSON object to an exported calculation function."""

    if not isinstance(payload, Mapping):
        raise TypeError("JSON request must be an object")
    operation = payload.get("operation")
    if not isinstance(operation, str) or not operation:
        raise ValueError("JSON request requires a non-empty 'operation' string")

    if "arguments" in payload:
        mixed_keys = set(payload) - {"operation", "arguments"}
        if mixed_keys:
            raise ValueError(
                "mixed top-level fields and 'arguments' are not allowed: "
                + ", ".join(sorted(mixed_keys))
            )
        arguments = payload["arguments"]
        if not isinstance(arguments, Mapping):
            raise TypeError("'arguments' must be a JSON object")
        arguments = dict(arguments)
    else:
        arguments = {key: value for key, value in payload.items() if key != "operation"}

    operations = {
        "classify_auction_phase": classify_auction_phase,
        "calculate_net_reward_risk": calculate_net_reward_risk,
        "calculate_break_even_win_rate": calculate_break_even_win_rate,
        "classify_environment_state": classify_environment_state,
        "evaluate_new_position_plan": evaluate_new_position_plan,
        "get_environment_policy": get_environment_policy,
        "calculate_overnight_notional_cap": calculate_overnight_notional_cap,
        "get_risk_tier_config": get_risk_tier_config,
        "evaluate_setup_tier": evaluate_setup_tier,
        "daily_lock_triggered": daily_lock_triggered,
        "weekly_loss_action": weekly_loss_action,
    }
    try:
        function = operations[operation]
    except KeyError as exc:
        allowed = ", ".join(sorted(operations))
        raise ValueError(
            f"unknown operation {operation!r}; expected one of: {allowed}"
        ) from exc

    if operation == "classify_auction_phase" and "current_time" in arguments:
        arguments["current_time"] = _parse_json_time(arguments["current_time"])
    return function(**arguments)


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(f"argument error: {message}")


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key!r}")
        result[key] = value
    return result


def _read_json_request(json_file: Optional[str]) -> Any:
    if json_file is None or json_file == "-":
        return json.load(sys.stdin, object_pairs_hook=_reject_duplicate_json_keys)
    with Path(json_file).open("r", encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=_reject_duplicate_json_keys)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _JsonArgumentParser(
        description="Calculate a local A-share pre-open trade-plan value from JSON.",
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        help="JSON request file; omit or use '-' to read stdin",
    )

    try:
        args = parser.parse_args(argv)
        request = _read_json_request(args.json_file)
        result = dispatch_json_request(request)
        response = {"ok": True, "result": result}
        exit_code = 0
    except Exception as exc:  # Convert malformed input into a machine-readable error.
        response = {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        exit_code = 2

    json.dump(
        response,
        sys.stdout,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
