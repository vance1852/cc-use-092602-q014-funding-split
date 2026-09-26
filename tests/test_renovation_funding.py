from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from renovation_funding.api import JsonApplication
from renovation_funding.clock import FrozenClock
from renovation_funding.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from renovation_funding.planning import (
    BatchSpec,
    InsufficientFunds,
    RuleSpec,
    compute_allocation,
    distribute_writeoff,
)
from renovation_funding.service import FundingService


def batch(
    batch_id: str,
    source: str,
    available: str,
    cap: str = "10000",
    types: frozenset[str] = frozenset({"dibao"}),
    scopes: frozenset[str] = frozenset({"rebuild"}),
    valid_from: date = date(2026, 1, 1),
    valid_to: date = date(2026, 12, 31),
) -> BatchSpec:
    return BatchSpec(
        batch_id=batch_id,
        source=source,
        available=Decimal(available),
        per_household_cap=Decimal(cap),
        household_types=types,
        scopes=scopes,
        valid_from=valid_from,
        valid_to=valid_to,
    )


RULE = RuleSpec(
    rule_id="funding-rule",
    version=1,
    source_order=("central", "provincial", "county"),
    source_caps={"central": Decimal("50"), "provincial": Decimal("30"), "county": Decimal("20")},
    household_residual=True,
)


class PlanningTests(unittest.TestCase):
    def test_allocation_hits_each_binding_and_stays_deterministic(self) -> None:
        batches = [
            batch("central-b", "central", "500000", cap="30000"),
            batch("province-b", "provincial", "300000", cap="20000"),
            batch("county-b", "county", "100000", cap="10000"),
            batch("county-a", "county", "4000", cap="10000"),
        ]
        lines = compute_allocation(
            estimated_cost=Decimal("65000"),
            household_type="dibao",
            scope="rebuild",
            as_of=date(2026, 9, 26),
            rule=RULE,
            batches=batches,
        )
        again = compute_allocation(
            estimated_cost=Decimal("65000"),
            household_type="dibao",
            scope="rebuild",
            as_of=date(2026, 9, 26),
            rule=RULE,
            batches=list(reversed(batches)),
        )
        self.assertEqual(lines, again)
        amounts = [(line.source, line.batch_id, str(line.amount)) for line in lines]
        self.assertEqual(
            amounts,
            [
                ("central", "central-b", "30000.00"),
                ("provincial", "province-b", "19500.00"),
                ("county", "county-a", "4000.00"),
                ("county", "county-b", "9000.00"),
                ("household", None, "2500.00"),
            ],
        )
        self.assertEqual(lines[0].bindings, ("household_cap",))
        self.assertEqual(lines[1].bindings, ("source_cap",))
        self.assertEqual(lines[2].bindings, ("batch_available",))
        self.assertEqual(lines[3].bindings, ("source_cap",))
        self.assertIn("每户封顶", lines[0].explanation)
        self.assertIn("家庭自筹", lines[4].explanation)

    def test_batches_are_used_by_expiry_then_identifier(self) -> None:
        rule = RuleSpec("r", 1, ("county",), {}, True)
        batches = [
            batch("county-z", "county", "100000", valid_to=date(2026, 12, 31)),
            batch("county-a", "county", "100000", valid_to=date(2026, 6, 30)),
        ]
        lines = compute_allocation(
            estimated_cost=Decimal("5000"),
            household_type="dibao",
            scope="rebuild",
            as_of=date(2026, 3, 1),
            rule=rule,
            batches=batches,
        )
        self.assertEqual(lines[0].batch_id, "county-a")

    def test_ineligible_batches_are_skipped(self) -> None:
        rule = RuleSpec("r", 1, ("central",), {}, True)
        batches = [
            batch("wrong-type", "central", "100000", types=frozenset({"tekun"})),
            batch("wrong-scope", "central", "100000", scopes=frozenset({"repair"})),
            batch("expired", "central", "100000", valid_to=date(2025, 12, 31)),
            batch("empty", "central", "0"),
        ]
        lines = compute_allocation(
            estimated_cost=Decimal("8000"),
            household_type="dibao",
            scope="rebuild",
            as_of=date(2026, 9, 26),
            rule=rule,
            batches=batches,
        )
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].source, "household")
        self.assertEqual(lines[0].amount, Decimal("8000.00"))

    def test_insufficient_funds_without_residual(self) -> None:
        rule = RuleSpec("r", 1, ("central",), {}, False)
        with self.assertRaises(InsufficientFunds):
            compute_allocation(
                estimated_cost=Decimal("8000"),
                household_type="dibao",
                scope="rebuild",
                as_of=date(2026, 9, 26),
                rule=rule,
                batches=[batch("central-a", "central", "1000")],
            )

    def test_writeoff_is_sequential_and_reports_excess(self) -> None:
        changes, excess = distribute_writeoff(
            [(1, Decimal("30000")), (2, Decimal("19500")), (3, Decimal("10000"))],
            Decimal("50000"),
        )
        self.assertEqual(
            [(c["seq"], str(c["write_off"]), str(c["release"])) for c in changes],
            [(1, "30000.00", "0.00"), (2, "19500.00", "0.00"), (3, "500.00", "9500.00")],
        )
        self.assertEqual(excess, Decimal("0.00"))
        _, excess = distribute_writeoff([(1, Decimal("100"))], Decimal("130"))
        self.assertEqual(excess, Decimal("30.00"))


class FundingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = FundingService(self.connection, self.clock)
        self.service.create_user("clerk", "经办员", "clerk")
        self.service.create_user("funds-a", "资金管理甲", "fund_manager")
        self.service.create_user("funds-b", "资金管理乙", "fund_manager")
        self.service.create_user("audit", "审计员", "auditor")
        self.service.register_household("clerk", {"household_id": "h-001", "head_name": "张三", "household_type": "dibao", "village": "北部村"})
        self.service.register_household("clerk", {"household_id": "h-002", "head_name": "李四", "household_type": "general", "village": "东部村"})
        self.service.create_user("family-001", "张三家属", "family", "h-001")
        for payload in (
            {"batch_id": "central-2026", "source": "central", "title": "中央补助", "total_amount": "500000", "household_types": ["dibao", "tekun", "tuopin"], "scopes": ["rebuild", "reinforce"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "30000"},
            {"batch_id": "province-2026", "source": "provincial", "title": "省级配套", "total_amount": "300000", "household_types": ["dibao", "tekun", "tuopin", "general"], "scopes": ["rebuild", "reinforce", "repair"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "20000"},
            {"batch_id": "county-2026-a", "source": "county", "title": "县级甲批", "total_amount": "100000", "household_types": ["dibao", "tekun"], "scopes": ["rebuild"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "10000"},
            {"batch_id": "county-2026-b", "source": "county", "title": "县级乙批", "total_amount": "50000", "household_types": ["dibao", "tekun"], "scopes": ["rebuild"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "10000"},
            {"batch_id": "county-2025", "source": "county", "title": "县级2025", "total_amount": "40000", "household_types": ["dibao", "tekun"], "scopes": ["rebuild"], "valid_from": "2025-01-01", "valid_to": "2025-12-31", "per_household_cap": "10000"},
        ):
            self.service.register_batch("funds-a", payload)
        self.service.create_rule("funds-a", {"rule_id": "funding-rule", "source_order": ["central", "provincial", "county"], "source_caps": {"central": "50", "provincial": "30", "county": "20"}, "household_residual": True, "note": "2026年分摊规则"})
        self.service.activate_rule("funds-a", "funding-rule", 1)
        self.service.register_project("clerk", {"project_id": "p-001", "household_id": "h-001", "scope": "rebuild", "estimated_cost": "65000", "address": "北部村东头12号", "idempotency_key": "proj-key-001"})

    def tearDown(self) -> None:
        self.connection.close()

    def confirm_p001(self) -> dict[str, object]:
        return self.service.confirm_project("funds-a", "p-001", {"rule_id": "funding-rule", "idempotency_key": "confirm-001"})

    def test_confirm_freezes_deterministic_explainable_allocation(self) -> None:
        result = self.confirm_p001()
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["rule_version"], 1)
        self.assertEqual(result["total_frozen"], "62500.00")
        self.assertEqual(result["household_commitment"], "2500.00")
        amounts = [(line["source"], line["batch_id"], line["amount"]) for line in result["lines"]]
        self.assertEqual(
            amounts,
            [
                ("central", "central-2026", "30000.00"),
                ("provincial", "province-2026", "19500.00"),
                ("county", "county-2026-a", "10000.00"),
                ("county", "county-2026-b", "3000.00"),
                ("household", None, "2500.00"),
            ],
        )
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertIn("每户封顶 30000.00", result["lines"][0]["explanation"])
        self.assertIn("低保户", result["lines"][0]["explanation"])
        balances = {
            row["batch_id"]: (row["available_amount"], row["frozen_amount"])
            for row in self.connection.execute("SELECT batch_id,available_amount,frozen_amount FROM fund_batches")
        }
        self.assertEqual(balances["central-2026"], ("470000.00", "30000.00"))
        self.assertEqual(balances["province-2026"], ("280500.00", "19500.00"))
        self.assertEqual(balances["county-2026-a"], ("90000.00", "10000.00"))
        self.assertEqual(balances["county-2026-b"], ("47000.00", "3000.00"))

    def test_confirm_replay_is_stable_and_payload_conflict_is_rejected(self) -> None:
        first = self.confirm_p001()
        self.assertEqual(first, self.confirm_p001())
        with self.assertRaises(Conflict):
            self.service.confirm_project("funds-a", "p-001", {"rule_id": "funding-rule", "idempotency_key": "confirm-001", "note": "篡改"})
        with self.assertRaises(InvalidState):
            self.service.confirm_project("funds-a", "p-001", {"rule_id": "funding-rule", "idempotency_key": "confirm-002"})

    def test_register_project_replay_and_conflict(self) -> None:
        payload = {"project_id": "p-900", "household_id": "h-001", "scope": "repair", "estimated_cost": "9000", "address": "北部村", "idempotency_key": "proj-key-900"}
        first = self.service.register_project("clerk", payload)
        self.assertEqual(first, self.service.register_project("clerk", payload))
        with self.assertRaises(Conflict):
            self.service.register_project("clerk", dict(payload, estimated_cost="9100"))

    def test_rule_revision_does_not_rewrite_confirmed_projects(self) -> None:
        self.confirm_p001()
        self.service.revise_rule("funds-a", "funding-rule", {"source_caps": {"central": "60", "provincial": "30", "county": "10"}})
        self.service.activate_rule("funds-a", "funding-rule", 2)
        self.service.register_project("clerk", {"project_id": "p-003", "household_id": "h-001", "scope": "rebuild", "estimated_cost": "65000", "address": "北部村", "idempotency_key": "proj-key-003"})
        second = self.service.confirm_project("funds-a", "p-003", {"rule_id": "funding-rule", "idempotency_key": "confirm-003"})
        self.assertEqual(second["rule_version"], 2)
        self.assertEqual(second["lines"][0]["amount"], "30000.00")
        view = self.service.get_project("funds-a", "p-001")
        self.assertEqual(view["rule_version"], 1)
        self.assertEqual(view["allocation"]["rule_version"], 1)
        self.assertEqual(view["allocation"]["lines"][0]["amount"], "30000.00")

    def test_confirm_requires_active_rule_and_sufficient_funds(self) -> None:
        self.service.create_rule("funds-a", {"rule_id": "strict-rule", "source_order": ["central"], "source_caps": {"central": "10"}, "household_residual": False, "note": "不允许自筹"})
        with self.assertRaises(InvalidState):
            self.service.confirm_project("funds-a", "p-001", {"rule_id": "strict-rule", "idempotency_key": "confirm-x1"})
        self.service.activate_rule("funds-a", "strict-rule", 1)
        with self.assertRaises(InvalidState):
            self.service.confirm_project("funds-a", "p-001", {"rule_id": "strict-rule", "idempotency_key": "confirm-x2"})

    def test_partial_settlement_writes_off_and_releases(self) -> None:
        self.confirm_p001()
        result = self.service.settle_project("funds-a", "p-001", {"outcome": "partial", "accepted_amount": "50000", "idempotency_key": "settle-1"})
        self.assertEqual(result["state"], "partially_completed")
        self.assertEqual(result["written_total"], "50000.00")
        self.assertEqual(result["released_total"], "15000.00")
        writes = [(line["source"], line["write_off"], line["release"]) for line in result["lines"]]
        self.assertEqual(
            writes,
            [
                ("central", "30000.00", "0.00"),
                ("provincial", "19500.00", "0.00"),
                ("county", "500.00", "9500.00"),
                ("county", "0.00", "3000.00"),
                ("household", "0.00", "2500.00"),
            ],
        )
        county_a = self.service.get_batch("funds-a", "county-2026-a")
        self.assertEqual(county_a["spent_amount"], "500.00")
        self.assertEqual(county_a["available_amount"], "99500.00")
        county_b = self.service.get_batch("funds-a", "county-2026-b")
        self.assertEqual(county_b["spent_amount"], "0.00")
        self.assertEqual(county_b["available_amount"], "50000.00")
        self.assertEqual(self.service.get_project("audit", "p-001")["settlement"]["outcome"], "partial")

    def test_completed_settlement_requires_full_acceptance(self) -> None:
        self.confirm_p001()
        with self.assertRaises(ValidationFailed):
            self.service.settle_project("funds-a", "p-001", {"outcome": "completed", "accepted_amount": "60000", "idempotency_key": "settle-bad"})
        result = self.service.settle_project("funds-a", "p-001", {"outcome": "completed", "accepted_amount": "65000", "idempotency_key": "settle-ok"})
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["written_total"], "65000.00")
        self.assertEqual(result["released_total"], "0.00")
        self.assertEqual(self.service.get_batch("audit", "central-2026")["spent_amount"], "30000.00")

    def test_cancel_and_failure_release_everything(self) -> None:
        self.confirm_p001()
        with self.assertRaises(ValidationFailed):
            self.service.settle_project("funds-a", "p-001", {"outcome": "failed", "accepted_amount": "100", "idempotency_key": "settle-bad2"})
        result = self.service.settle_project("funds-a", "p-001", {"outcome": "cancelled", "accepted_amount": "0", "idempotency_key": "settle-c"})
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["written_total"], "0.00")
        self.assertEqual(result["released_total"], "65000.00")
        self.assertEqual(self.service.get_batch("funds-a", "central-2026")["available_amount"], "500000.00")
        self.assertEqual(self.service.get_batch("funds-a", "central-2026")["frozen_amount"], "0.00")

    def test_settlement_replay_stable_and_content_conflict(self) -> None:
        self.confirm_p001()
        payload = {"outcome": "partial", "accepted_amount": "50000", "idempotency_key": "settle-9"}
        first = self.service.settle_project("funds-a", "p-001", payload)
        self.assertEqual(first, self.service.settle_project("funds-a", "p-001", payload))
        with self.assertRaises(Conflict):
            self.service.settle_project("funds-a", "p-001", {"outcome": "partial", "accepted_amount": "51000", "idempotency_key": "settle-9"})
        with self.assertRaises(InvalidState):
            self.service.settle_project("funds-a", "p-001", {"outcome": "partial", "accepted_amount": "50000", "idempotency_key": "settle-10"})

    def test_adjustment_requires_second_reviewer_and_forms_new_version(self) -> None:
        self.confirm_p001()
        proposal = {"adjustment_id": "adj-1", "reason": "县级甲批被收回", "lines": [
            {"batch_id": "central-2026", "amount": "30000"},
            {"batch_id": "province-2026", "amount": "19500"},
            {"batch_id": "county-2026-b", "amount": "10000"},
            {"batch_id": None, "amount": "5500"},
        ]}
        created = self.service.propose_adjustment("funds-a", "p-001", proposal)
        self.assertEqual(created["state"], "pending")
        with self.assertRaises(Forbidden):
            self.service.review_adjustment("funds-a", "adj-1", {"decision": "approve"})
        approved = self.service.review_adjustment("funds-b", "adj-1", {"decision": "approve"})
        self.assertEqual(approved["allocation_version"], 2)
        view = self.service.get_project("funds-a", "p-001")
        self.assertEqual(view["allocation"]["version"], 2)
        total = sum(Decimal(line["amount"]) for line in view["allocation"]["lines"])
        self.assertEqual(total, Decimal("65000.00"))
        self.assertIn("双人复核", view["allocation"]["lines"][2]["explanation"])
        county_a = self.service.get_batch("funds-a", "county-2026-a")
        self.assertEqual((county_a["available_amount"], county_a["frozen_amount"]), ("100000.00", "0.00"))
        county_b = self.service.get_batch("funds-a", "county-2026-b")
        self.assertEqual((county_b["available_amount"], county_b["frozen_amount"]), ("40000.00", "10000.00"))
        with self.assertRaises(InvalidState):
            self.service.review_adjustment("funds-b", "adj-1", {"decision": "approve"})

    def test_adjustment_reject_keeps_existing_version(self) -> None:
        self.confirm_p001()
        self.service.propose_adjustment("funds-a", "p-001", {"adjustment_id": "adj-2", "reason": "试算", "lines": [
            {"batch_id": "central-2026", "amount": "30000"},
            {"batch_id": "province-2026", "amount": "19500"},
            {"batch_id": "county-2026-a", "amount": "10000"},
            {"batch_id": "county-2026-b", "amount": "3000"},
            {"batch_id": None, "amount": "2500"},
        ]})
        rejected = self.service.review_adjustment("funds-b", "adj-2", {"decision": "reject"})
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(self.service.get_project("funds-a", "p-001")["allocation"]["version"], 1)

    def test_adjustment_validates_total_cap_and_eligibility(self) -> None:
        self.confirm_p001()
        with self.assertRaises(ValidationFailed):
            self.service.propose_adjustment("funds-a", "p-001", {"adjustment_id": "adj-3", "reason": "总额不一致", "lines": [
                {"batch_id": "central-2026", "amount": "30000"},
                {"batch_id": None, "amount": "30000"},
            ]})
        with self.assertRaises(ValidationFailed):
            self.service.propose_adjustment("funds-a", "p-001", {"adjustment_id": "adj-4", "reason": "超过每户封顶", "lines": [
                {"batch_id": "central-2026", "amount": "31000"},
                {"batch_id": "province-2026", "amount": "19500"},
                {"batch_id": "county-2026-a", "amount": "10000"},
                {"batch_id": "county-2026-b", "amount": "3000"},
                {"batch_id": None, "amount": "1500"},
            ]})
        with self.assertRaises(ValidationFailed):
            self.service.propose_adjustment("funds-a", "p-001", {"adjustment_id": "adj-5", "reason": "批次已到期", "lines": [
                {"batch_id": "central-2026", "amount": "30000"},
                {"batch_id": "province-2026", "amount": "19500"},
                {"batch_id": "county-2025", "amount": "10000"},
                {"batch_id": "county-2026-b", "amount": "3000"},
                {"batch_id": None, "amount": "4500"},
            ]})

    def test_carry_forward_moves_expired_balance(self) -> None:
        result = self.service.carry_forward_batch("funds-a", "county-2025", {"new_batch_id": "county-2026-c", "valid_from": "2026-10-01", "valid_to": "2027-03-31", "idempotency_key": "carry-1"})
        self.assertEqual(result["amount"], "40000.00")
        old = self.service.get_batch("funds-a", "county-2025")
        self.assertEqual((old["state"], old["available_amount"], old["carried_out"]), ("carried", "0.00", "40000.00"))
        new = self.service.get_batch("funds-a", "county-2026-c")
        self.assertEqual((new["total_amount"], new["available_amount"], new["source"]), ("40000.00", "40000.00", "county"))
        self.assertEqual(result, self.service.carry_forward_batch("funds-a", "county-2025", {"new_batch_id": "county-2026-c", "valid_from": "2026-10-01", "valid_to": "2027-03-31", "idempotency_key": "carry-1"}))
        with self.assertRaises(InvalidState):
            self.service.carry_forward_batch("funds-a", "central-2026", {"new_batch_id": "central-2027", "valid_from": "2027-01-01", "valid_to": "2027-12-31", "idempotency_key": "carry-2"})

    def test_family_sees_only_own_authorized_details(self) -> None:
        self.confirm_p001()
        self.service.register_project("clerk", {"project_id": "p-002", "household_id": "h-002", "scope": "repair", "estimated_cost": "20000", "address": "东部村", "idempotency_key": "proj-key-002"})
        own = self.service.get_project("family-001", "p-001")
        self.assertEqual(own["household_id"], "h-001")
        self.assertTrue(all("batch_id" not in line for line in own["allocation"]["lines"]))
        self.assertNotIn("input_sha256", own["allocation"])
        with self.assertRaises(Forbidden):
            self.service.get_project("family-001", "p-002")
        with self.assertRaises(Forbidden):
            self.service.get_batch("family-001", "central-2026")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("family-001")
        manager = self.service.get_project("funds-a", "p-001")
        self.assertIn("batch_id", manager["allocation"]["lines"][0])
        self.assertIn("input_sha256", manager["allocation"])

    def test_role_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.confirm_project("clerk", "p-001", {"rule_id": "funding-rule", "idempotency_key": "confirm-c"})
        with self.assertRaises(Forbidden):
            self.service.register_batch("clerk", {"batch_id": "x-1", "source": "central", "title": "越权", "total_amount": "1", "household_types": ["dibao"], "scopes": ["rebuild"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "1"})
        with self.assertRaises(Forbidden):
            self.service.register_batch("audit", {"batch_id": "x-2", "source": "central", "title": "越权", "total_amount": "1", "household_types": ["dibao"], "scopes": ["rebuild"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "1"})
        with self.assertRaises(Forbidden):
            self.service.audit_chain("clerk")
        self.assertEqual(self.service.get_batch("audit", "central-2026")["batch_id"], "central-2026")

    def test_money_conservation_holds_across_lifecycle(self) -> None:
        self.confirm_p001()
        self.service.settle_project("funds-a", "p-001", {"outcome": "partial", "accepted_amount": "50000", "idempotency_key": "settle-m"})
        self.service.carry_forward_batch("funds-a", "county-2025", {"new_batch_id": "county-2026-c", "valid_from": "2026-10-01", "valid_to": "2027-03-31", "idempotency_key": "carry-m"})
        for row in self.connection.execute("SELECT * FROM fund_batches").fetchall():
            total = Decimal(row["total_amount"])
            parts = sum(Decimal(row[key]) for key in ("available_amount", "frozen_amount", "spent_amount", "carried_out"))
            self.assertEqual(total, parts, row["batch_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.confirm_p001()
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE funding_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(FundingService(self.connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_header_is_required(self) -> None:
        response = self.app.handle("POST", "/users", body=json.dumps({"user_id": "u1", "display_name": "经办", "role": "clerk"}).encode())
        self.assertEqual(response.status, 201)
        denied = self.app.handle("GET", "/batches")
        self.assertEqual(denied.status, 422)
        self.assertEqual(denied.body["error"]["code"], "validation_failed")

    def test_unknown_route_and_error_shape(self) -> None:
        self.app.handle("POST", "/users", body=json.dumps({"user_id": "u1", "display_name": "经办", "role": "clerk"}).encode())
        missing = self.app.handle("GET", "/nope", {"X-Actor-Id": "u1"})
        self.assertEqual(missing.status, 404)
        forbidden = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "u1"})
        self.assertEqual(forbidden.status, 403)
        self.assertEqual(forbidden.body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
