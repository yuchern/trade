from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / "a-share-ultrashort-preopen"

REQUIRED_RELATIVE_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "scripts/calc_trade_plan.py",
    "references/decision-framework.md",
    "references/risk-model.md",
    "references/input-output.md",
    "references/evidence-and-rules.md",
)

BANNED_IMPORT_ROOTS = {
    "aiohttp",
    "akshare",
    "alpaca_trade_api",
    "baostock",
    "binance",
    "ccxt",
    "easytrader",
    "efinance",
    "ftplib",
    "futu",
    "http",
    "httpx",
    "ib_insync",
    "ibapi",
    "paramiko",
    "pytdx",
    "requests",
    "smtplib",
    "socket",
    "subprocess",
    "tqsdk",
    "tradeapi",
    "tushare",
    "urllib",
    "vnpy",
    "webbrowser",
    "websocket",
    "websockets",
    "xtquant",
}

BANNED_CALL_NAMES = {
    "__import__",
    "cancel_order",
    "create_connection",
    "import_module",
    "insert_order",
    "order_insert",
    "place_order",
    "popen",
    "send_order",
    "submit_order",
    "system",
    "urlopen",
    "urlretrieve",
}

MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
BUNDLED_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"((?:\./)?(?:agents|references|scripts)/[A-Za-z0-9_./-]+)"
)


def _top_level_yaml_section(text: str, name: str) -> str | None:
    lines = text.splitlines()
    header = re.compile(rf"^{re.escape(name)}:\s*(?:#.*)?$")

    for index, line in enumerate(lines):
        if not header.fullmatch(line):
            continue

        body: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate and not candidate[0].isspace():
                break
            body.append(candidate)
        return "\n".join(body)

    return None


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class SkillContractTests(unittest.TestCase):
    def _require_file(self, relative_path: str) -> Path:
        path = SKILL_ROOT / relative_path
        if not path.is_file():
            self.skipTest(f"contract target is not implemented yet: {relative_path}")
        return path

    def _calculator_tree(self) -> ast.Module:
        script = self._require_file("scripts/calc_trade_plan.py")
        source = script.read_text(encoding="utf-8")
        try:
            return ast.parse(source, filename=str(script))
        except SyntaxError as error:
            self.fail(f"calc_trade_plan.py must be valid Python: {error}")

    def test_skill_package_has_required_structure(self) -> None:
        self.assertTrue(
            SKILL_ROOT.is_dir(),
            f"production skill directory is missing: {SKILL_ROOT}",
        )

        missing = [
            relative_path
            for relative_path in REQUIRED_RELATIVE_FILES
            if not (SKILL_ROOT / relative_path).is_file()
        ]
        self.assertEqual(
            missing,
            [],
            "required skill files are missing:\n- " + "\n- ".join(missing),
        )

    def test_metadata_disables_implicit_invocation(self) -> None:
        metadata = self._require_file("agents/openai.yaml").read_text(encoding="utf-8")
        policy = _top_level_yaml_section(metadata, "policy")

        self.assertIsNotNone(policy, "openai.yaml must contain a top-level policy section")
        self.assertRegex(
            policy or "",
            r"(?m)^\s+allow_implicit_invocation:\s*false\s*(?:#.*)?$",
            "policy.allow_implicit_invocation must be the boolean false",
        )

    def test_default_prompt_names_the_explicit_skill(self) -> None:
        metadata = self._require_file("agents/openai.yaml").read_text(encoding="utf-8")
        interface = _top_level_yaml_section(metadata, "interface")

        self.assertIsNotNone(interface, "openai.yaml must contain a top-level interface section")
        prompt = re.search(
            r"(?m)^\s+default_prompt:\s*(.+?)\s*(?:#.*)?$",
            interface or "",
        )
        self.assertIsNotNone(prompt, "interface.default_prompt must be present")
        self.assertIn(
            "$a-share-ultrashort-preopen",
            prompt.group(1) if prompt else "",
            "default_prompt must explicitly mention $a-share-ultrashort-preopen",
        )

    def test_every_local_reference_in_skill_markdown_exists(self) -> None:
        skill = self._require_file("SKILL.md")
        text = skill.read_text(encoding="utf-8")

        raw_targets = [match.group(1) for match in MARKDOWN_LINK_RE.finditer(text)]
        raw_targets.extend(match.group(1) for match in BUNDLED_PATH_RE.finditer(text))

        missing: set[str] = set()
        escaping: set[str] = set()
        for raw_target in raw_targets:
            target = raw_target.strip()
            if target.startswith("<") and ">" in target:
                target = target[1 : target.index(">")]
            else:
                target = target.split(maxsplit=1)[0]

            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
                continue

            relative_path = unquote(parsed.path).removeprefix("./").rstrip(".,;:")
            resolved = (SKILL_ROOT / relative_path).resolve()
            try:
                resolved.relative_to(SKILL_ROOT.resolve())
            except ValueError:
                escaping.add(relative_path)
                continue

            if not resolved.exists():
                missing.add(relative_path)

        self.assertEqual(
            sorted(escaping),
            [],
            "SKILL.md local references must stay inside the skill directory",
        )
        self.assertEqual(
            sorted(missing),
            [],
            "SKILL.md contains missing local references: " + ", ".join(sorted(missing)),
        )

    def test_calculator_has_no_network_or_broker_imports(self) -> None:
        tree = self._calculator_tree()
        imported_roots: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])

        forbidden = sorted(imported_roots & BANNED_IMPORT_ROOTS)
        self.assertEqual(
            forbidden,
            [],
            "calculator must remain offline and broker-independent; forbidden imports: "
            + ", ".join(forbidden),
        )

    def test_calculator_cannot_make_network_or_order_calls(self) -> None:
        tree = self._calculator_tree()
        forbidden = sorted(
            {
                call_name
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                if (call_name := _call_name(node)) in BANNED_CALL_NAMES
            }
        )

        self.assertEqual(
            forbidden,
            [],
            "calculator must not make network, shell, or order-submission calls: "
            + ", ".join(forbidden),
        )

    def test_skill_requires_the_atomic_new_position_operation(self) -> None:
        skill = self._require_file("SKILL.md").read_text(encoding="utf-8")
        calculator = self._require_file("scripts/calc_trade_plan.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("evaluate_new_position_plan", skill)
        self.assertRegex(
            calculator,
            r'"evaluate_new_position_plan"\s*:\s*evaluate_new_position_plan',
        )
        self.assertNotRegex(
            calculator,
            r'"calculate_position_shares"\s*:\s*calculate_position_shares',
            "unsafe low-level sizing must not be exposed by the CLI dispatcher",
        )

    def test_reference_contract_preserves_the_weekly_recovery_state(self) -> None:
        risk_model = self._require_file("references/risk-model.md").read_text(
            encoding="utf-8"
        )
        input_contract = self._require_file("references/input-output.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("weekly_lock_remainder_of_week", risk_model)
        self.assertIn("reduced_risk_trading_days_remaining", risk_model)
        self.assertIn("weekly_recovery_days_remaining", input_contract)
        self.assertIn("0.375%", risk_model)
        self.assertIn("0.75%", risk_model)

    def test_reference_contract_forces_retreat_and_other_falling_states_to_defense(self) -> None:
        framework = self._require_file("references/decision-framework.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("指数上涨不能覆盖情绪退潮", framework)
        self.assertIn("下跌阶段除上一条完整例外外一律防守", framework)
        self.assertIn("icepoint_repair", framework)

    def test_reference_contract_uses_auditable_history_and_real_order_grid(self) -> None:
        input_contract = self._require_file("references/input-output.md").read_text(
            encoding="utf-8"
        )

        for required in (
            "closed=true",
            "costs_included=true",
            "history_cutoff_date",
            "exit_timestamp",
            "planned_stop_price",
            "planned_slippage_rate_per_side",
            "commission_rate",
            "commission_basis",
            "setup_equity_before",
            "setup_equity_after",
            "minimum_order_shares",
            "share_increment",
            "OPEN_ORDER_EXPOSURE_UNRESOLVED",
        ):
            self.assertIn(required, input_contract)

    def test_exact_plan_contract_cannot_understate_market_rules_or_costs(self) -> None:
        risk_model = self._require_file("references/risk-model.md").read_text(
            encoding="utf-8"
        )
        evidence = self._require_file("references/evidence-and-rules.md").read_text(
            encoding="utf-8"
        )
        calculator = self._require_file("scripts/calc_trade_plan.py").read_text(
            encoding="utf-8"
        )

        for required in (
            '"limit_down_fraction": Decimal("0.10")',
            '"limit_down_fraction": Decimal("0.20")',
            '_MINIMUM_SELL_TAX_RATE = Decimal("0.0005")',
            '_MINIMUM_SLIPPAGE_RATE = Decimal("0.0005")',
            '_MAXIMUM_PLANNED_SLIPPAGE_RATE = Decimal("0.05")',
            '_MINIMUM_COMMISSION_PER_SIDE = Decimal("5.00")',
            '_MAXIMUM_COMMISSION_RATE = Decimal("0.003")',
            '_MINIMUM_ALL_IN_FEE_RATE = Decimal("0.0000641")',
            '_PRICE_TICK = Decimal("0.01")',
            "_derive_market_segment",
            "rule_version_checked_at",
            "rules_match_bundled_configuration",
            "_reject_duplicate_json_keys",
            "_calculate_overnight_notional_cap_decimal",
        ):
            self.assertIn(required, calculator)
        self.assertIn("existing_symbol_notional", risk_model)
        self.assertIn("existing_symbol_stress_fraction", risk_model)
        self.assertIn("latest_completed_trading_date", risk_model)
        self.assertIn("bundled_rules_as_of", risk_model)
        self.assertIn("0.0000641", risk_model)
        self.assertIn("财政部、税务总局关于减半征收证券交易印花税", evidence)

    def test_exact_plan_contract_binds_auction_scope_and_history_scope(self) -> None:
        skill = self._require_file("SKILL.md").read_text(encoding="utf-8")
        risk_model = self._require_file("references/risk-model.md").read_text(
            encoding="utf-8"
        )
        input_contract = self._require_file("references/input-output.md").read_text(
            encoding="utf-8"
        )
        calculator = self._require_file("scripts/calc_trade_plan.py").read_text(
            encoding="utf-8"
        )

        for required in (
            "decision_time",
            "data_cutoff",
            "final_auction_data_confirmed",
            "final_auction_price",
            "forbidden_chase_price",
            "effective_entry_price",
            "existing_ice_trial_plan_count",
            "trading_session_confirmed",
            "trading_session_independently_verified",
            "FINAL_AUCTION_ABOVE_CHASE_LIMIT",
            "ICE_TRIAL_PLAN_SLOT_OCCUPIED",
        ):
            self.assertIn(required, input_contract)
            self.assertIn(required, calculator)
        self.assertIn("09:25–09:29:59", skill)
        self.assertIn("bundled_rules_as_of<=decision_date", risk_model)
        self.assertIn("symbol, exchange", input_contract)
        self.assertIn("_require_realistic_exact_plan_costs", calculator)


if __name__ == "__main__":
    unittest.main()
