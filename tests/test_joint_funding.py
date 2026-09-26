from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from joint_funding.acceptance import run as acceptance_run
from joint_funding.api import JsonApplication
from joint_funding.clock import FrozenClock
from joint_funding.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from joint_funding.service import FundingService
from joint_funding.sharing import BatchSlice, allocate_cost, settle_cost


ROOT = Path(__file__).resolve().parents[1]


class SharingTests(unittest.TestCase):
    def test_allocation_is_deterministic_and_explainable(self) -> None:
        batches = [
            BatchSlice("central-2026", "central", Decimal("20000"), Decimal("500000"), 10, "2026-01-01"),
            BatchSlice("county-2026", "county", Decimal("10000"), Decimal("8000"), 30, "2026-01-01"),
        ]
        steps = ["central", "county"]
        first = allocate_cost(Decimal("35000"), steps, batches)
        second = allocate_cost(Decimal("35000"), steps, batches)
        self.assertEqual(first, second)
        self.assertEqual(first["lines"][0]["binding_constraint"], "per_household_cap")
        self.assertEqual(first["lines"][1]["amount_cny"], "8000.00")
        self.assertEqual(first["lines"][1]["binding_constraint"], "batch_available")
        self.assertEqual(first["public_total_cny"], "28000.00")
        self.assertEqual(first["household_share_cny"], "7000.00")

    def test_settle_writes_off_actual_and_returns_remainder(self) -> None:
        allocation = allocate_cost(
            Decimal("45000"),
            ["central", "county"],
            [
                BatchSlice("central-2026", "central", Decimal("20000"), Decimal("500000"), 10, "2026-01-01"),
                BatchSlice("county-2026", "county", Decimal("10000"), Decimal("200000"), 30, "2026-01-01"),
            ],
        )
        result = settle_cost(Decimal("24000"), ["central", "county"], allocation["lines"])
        write_offs = {line["batch_id"]: line["written_off_cny"] for line in result["write_offs"]}
        self.assertEqual(write_offs, {"central-2026": "20000.00", "county-2026": "4000.00"})
        self.assertEqual(result["remainders"], [
            {"batch_id": "county-2026", "source": "county", "remainder_cny": "6000.00"}
        ])
        self.assertEqual(result["remainder_total_cny"], "6000.00")

    def test_settle_above_frozen_caps_public_share(self) -> None:
        allocation = allocate_cost(
            Decimal("30000"),
            ["central"],
            [BatchSlice("central-2026", "central", Decimal("20000"), Decimal("500000"), 10, "2026-01-01")],
        )
        result = settle_cost(Decimal("50000"), ["central"], allocation["lines"])
        self.assertEqual(result["public_total_cny"], "20000.00")
        self.assertEqual(result["household_share_cny"], "30000.00")
        self.assertEqual(result["remainders"], [])


class FundingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = FundingService(self.connection, self.clock)
        for user_id, role in (
            ("clerk-1", "clerk"),
            ("fund-1", "fund_manager"),
            ("fund-2", "fund_manager"),
            ("household-east", "household"),
            ("household-west", "household"),
            ("audit-1", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_batch("fund-1", {
            "batch_id": "central-2026", "source": "central", "name": "中央补助",
            "total_cny": "500000", "household_types": ["minimum-living", "extreme-hardship"],
            "scopes": ["rebuild", "reinforce"], "valid_from": "2026-01-01", "valid_to": "2026-12-31",
            "per_household_cap_cny": "20000", "priority": 10,
        })
        self.service.register_batch("fund-1", {
            "batch_id": "province-2026", "source": "provincial", "name": "省级配套",
            "total_cny": "300000", "household_types": ["minimum-living", "poverty-lifted"],
            "scopes": ["rebuild", "reinforce"], "valid_from": "2026-01-01", "valid_to": "2026-12-31",
            "per_household_cap_cny": "15000", "priority": 20,
        })
        self.service.register_batch("fund-1", {
            "batch_id": "county-2026", "source": "county", "name": "县级资金",
            "total_cny": "200000", "household_types": ["minimum-living", "general"],
            "scopes": ["rebuild", "reinforce", "partial-repair"], "valid_from": "2026-01-01",
            "valid_to": "2026-12-31", "per_household_cap_cny": "10000", "priority": 30,
        })
        self.service.register_rule("fund-1", {
            "rule_id": "joint-funding", "name": "中央省县依次分摊",
            "steps": ["central", "provincial", "county"],
        })

    def tearDown(self) -> None:
        self.connection.close()

    def register_project(self, project_id: str = "proj-1", household: str = "household-east",
                         household_type: str = "minimum-living", scope: str = "rebuild",
                         cost: str = "52000") -> dict:
        return self.service.register_project("clerk-1", {
            "project_id": project_id, "household_id": household, "household_type": household_type,
            "scope": scope, "estimated_cost_cny": cost,
        })

    def test_batch_registration_validates_terms(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("fund-1", {
                "batch_id": "bad-1", "source": "charity", "name": "未知来源", "total_cny": "100",
                "household_types": ["general"], "scopes": ["rebuild"], "valid_from": "2026-01-01",
                "valid_to": "2026-12-31", "per_household_cap_cny": "10", "priority": 10,
            })
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("fund-1", {
                "batch_id": "bad-2", "source": "central", "name": "封顶超额", "total_cny": "100",
                "household_types": ["general"], "scopes": ["rebuild"], "valid_from": "2026-01-01",
                "valid_to": "2026-12-31", "per_household_cap_cny": "200", "priority": 10,
            })
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("fund-1", {
                "batch_id": "bad-3", "source": "central", "name": "有效期倒置", "total_cny": "100",
                "household_types": ["general"], "scopes": ["rebuild"], "valid_from": "2026-12-31",
                "valid_to": "2026-01-01", "per_household_cap_cny": "10", "priority": 10,
            })

    def test_confirm_freezes_deterministic_explainable_allocation(self) -> None:
        self.register_project()
        confirmed = self.service.confirm_project("clerk-1", "proj-1")
        allocation = confirmed["allocation"]
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["rule_version"], 1)
        self.assertEqual(
            [(line["batch_id"], line["amount_cny"], line["binding_constraint"]) for line in allocation["lines"]],
            [
                ("central-2026", "20000.00", "per_household_cap"),
                ("province-2026", "15000.00", "per_household_cap"),
                ("county-2026", "10000.00", "per_household_cap"),
            ],
        )
        self.assertEqual(allocation["public_total_cny"], "45000.00")
        self.assertEqual(allocation["household_share_cny"], "7000.00")
        self.assertEqual(self.service.get_batch("fund-1", "central-2026")["frozen_cny"], "20000.00")
        self.assertEqual(self.service.get_batch("fund-1", "central-2026")["available_cny"], "480000.00")
        with self.assertRaises(InvalidState):
            self.service.confirm_project("clerk-1", "proj-1")

    def test_confirm_respects_household_type_scope_and_validity(self) -> None:
        self.service.register_batch("fund-1", {
            "batch_id": "county-expired", "source": "county", "name": "已到期批次",
            "total_cny": "90000", "household_types": ["minimum-living"], "scopes": ["rebuild"],
            "valid_from": "2025-01-01", "valid_to": "2025-12-31",
            "per_household_cap_cny": "9000", "priority": 5,
        })
        self.register_project(project_id="proj-general", household_type="general", scope="partial-repair", cost="25000")
        confirmed = self.service.confirm_project("clerk-1", "proj-general")
        self.assertEqual(
            [line["batch_id"] for line in confirmed["allocation"]["lines"]],
            ["county-2026"],
        )
        self.assertEqual(confirmed["allocation"]["household_share_cny"], "15000.00")

    def test_rule_revision_does_not_rewrite_confirmed_projects(self) -> None:
        self.register_project("proj-old")
        self.service.confirm_project("clerk-1", "proj-old")
        self.service.register_rule("fund-1", {
            "rule_id": "joint-funding", "name": "仅县级分摊", "steps": ["county"],
        })
        old = self.service.get_project("clerk-1", "proj-old")
        self.assertEqual(old["rule_version"], 1)
        self.assertEqual(len(old["allocation"]["lines"]), 3)
        self.assertEqual(old["allocation"]["public_total_cny"], "45000.00")
        self.register_project("proj-new", cost="25000")
        new = self.service.confirm_project("clerk-1", "proj-new")
        self.assertEqual(new["rule_version"], 2)
        self.assertEqual([line["batch_id"] for line in new["allocation"]["lines"]], ["county-2026"])
        settled = self.service.settle_project("clerk-1", {
            "settlement_id": "settle-old", "project_id": "proj-old", "actual_cost_cny": "45000",
            "remainder_policy": "release",
        })
        self.assertEqual(len(settled["write_offs"]), 3)

    def test_identical_rule_content_conflicts(self) -> None:
        with self.assertRaises(Conflict):
            self.service.register_rule("fund-1", {
                "rule_id": "joint-funding", "name": "中央省县依次分摊",
                "steps": ["central", "provincial", "county"],
            })

    def test_settle_partial_completion_releases_remainder(self) -> None:
        self.register_project()
        self.service.confirm_project("clerk-1", "proj-1")
        settled = self.service.settle_project("clerk-1", {
            "settlement_id": "settle-1", "project_id": "proj-1", "actual_cost_cny": "39000",
            "remainder_policy": "release",
        })
        self.assertEqual(settled["outcome"], "completed_partial")
        self.assertEqual(settled["public_total_cny"], "39000.00")
        self.assertEqual(settled["remainders"], [
            {"batch_id": "county-2026", "source": "county", "remainder_cny": "6000.00"}
        ])
        county = self.service.get_batch("fund-1", "county-2026")
        self.assertEqual(county["frozen_cny"], "0.00")
        self.assertEqual(county["used_cny"], "4000.00")
        self.assertEqual(county["available_cny"], "196000.00")

    def test_settle_carry_forward_moves_remainder_to_target_batch(self) -> None:
        self.service.register_batch("fund-1", {
            "batch_id": "county-2027", "source": "county", "name": "下一年度结转",
            "total_cny": "1000", "household_types": ["general"], "scopes": ["partial-repair"],
            "valid_from": "2026-01-01", "valid_to": "2027-12-31",
            "per_household_cap_cny": "1000", "priority": 40,
        })
        self.register_project()
        self.service.confirm_project("clerk-1", "proj-1")
        settled = self.service.settle_project("clerk-1", {
            "settlement_id": "settle-1", "project_id": "proj-1", "actual_cost_cny": "39000",
            "remainder_policy": "carry_forward", "carry_forward_to": "county-2027",
        })
        self.assertEqual(settled["outcome"], "completed_partial")
        source = self.service.get_batch("fund-1", "county-2026")
        target = self.service.get_batch("fund-1", "county-2027")
        self.assertEqual(source["total_cny"], "194000.00")
        self.assertEqual(source["frozen_cny"], "0.00")
        self.assertEqual(target["total_cny"], "7000.00")
        self.assertEqual(target["transfers"][0]["from_batch_id"], "county-2026")
        self.assertEqual(target["transfers"][0]["amount_cny"], "6000.00")

    def test_settle_replay_is_stable_and_conflicting_payload_rejected(self) -> None:
        self.register_project()
        self.service.confirm_project("clerk-1", "proj-1")
        payload = {
            "settlement_id": "settle-1", "project_id": "proj-1", "actual_cost_cny": "46500",
            "remainder_policy": "release",
        }
        first = self.service.settle_project("clerk-1", payload)
        second = self.service.settle_project("clerk-1", dict(payload))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["settlement_id"], second["settlement_id"])
        self.assertEqual(first["write_offs"], second["write_offs"])
        with self.assertRaises(Conflict):
            self.service.settle_project("clerk-1", dict(payload, actual_cost_cny="46000"))
        with self.assertRaises(InvalidState):
            self.service.settle_project("clerk-1", {
                "settlement_id": "settle-2", "project_id": "proj-1", "actual_cost_cny": "1000",
                "remainder_policy": "release",
            })

    def test_cancel_and_fail_release_frozen_amounts(self) -> None:
        self.register_project("proj-cancel")
        self.service.confirm_project("clerk-1", "proj-cancel")
        cancelled = self.service.cancel_project("clerk-1", "proj-cancel")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.service.get_batch("fund-1", "central-2026")["frozen_cny"], "0.00")
        self.register_project("proj-fail")
        self.service.confirm_project("clerk-1", "proj-fail")
        failed = self.service.fail_project("clerk-1", "proj-fail", "验收不合格，需整改后重新申报")
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["failure_reason"], "验收不合格，需整改后重新申报")
        self.assertEqual(self.service.get_batch("fund-1", "central-2026")["frozen_cny"], "0.00")
        with self.assertRaises(InvalidState):
            self.service.cancel_project("clerk-1", "proj-cancel")

    def test_manual_adjustment_requires_dual_review_and_forms_new_version(self) -> None:
        self.register_project("proj-adj", cost="30000")
        self.service.confirm_project("clerk-1", "proj-adj")
        proposal = self.service.propose_adjustment("fund-1", {
            "adjustment_id": "adj-1", "project_id": "proj-adj",
            "lines": [
                {"batch_id": "central-2026", "amount_cny": "18000"},
                {"batch_id": "county-2026", "amount_cny": "6000"},
            ],
            "reason": "省级批次额度紧张，改由中央与县级承担",
        })
        self.assertEqual(proposal["state"], "pending")
        with self.assertRaises(Forbidden):
            self.service.approve_adjustment("fund-1", "adj-1")
        adjusted = self.service.approve_adjustment("fund-2", "adj-1")
        self.assertEqual(adjusted["allocation"]["version"], 2)
        self.assertEqual(adjusted["allocation"]["kind"], "manual")
        self.assertEqual(adjusted["allocation"]["approved_by"], "fund-2")
        self.assertEqual(adjusted["allocation"]["public_total_cny"], "24000.00")
        self.assertEqual(adjusted["allocation"]["household_share_cny"], "6000.00")
        self.assertEqual(self.service.get_batch("fund-1", "province-2026")["frozen_cny"], "0.00")
        self.assertEqual(self.service.get_batch("fund-1", "central-2026")["frozen_cny"], "18000.00")
        with self.assertRaises(InvalidState):
            self.service.reject_adjustment("fund-2", "adj-1")

    def test_adjustment_beyond_available_rolls_back(self) -> None:
        self.service.register_batch("fund-1", {
            "batch_id": "county-tiny", "source": "county", "name": "小额县级批次",
            "total_cny": "12000", "household_types": ["minimum-living"], "scopes": ["rebuild"],
            "valid_from": "2026-01-01", "valid_to": "2026-12-31",
            "per_household_cap_cny": "12000", "priority": 40,
        })
        self.register_project("proj-adj", cost="30000")
        self.service.confirm_project("clerk-1", "proj-adj")
        self.service.propose_adjustment("fund-1", {
            "adjustment_id": "adj-1", "project_id": "proj-adj",
            "lines": [{"batch_id": "county-tiny", "amount_cny": "25000"}],
            "reason": "尝试超出批次可用余额",
        })
        with self.assertRaises(Conflict):
            self.service.approve_adjustment("fund-2", "adj-1")
        project = self.service.get_project("clerk-1", "proj-adj")
        self.assertEqual(project["allocation"]["version"], 1)
        self.assertEqual(project["allocation"]["kind"], "rule")
        self.assertEqual(self.service.get_batch("fund-1", "central-2026")["frozen_cny"], "20000.00")
        self.assertEqual(self.service.get_batch("fund-1", "province-2026")["frozen_cny"], "10000.00")
        self.assertEqual(self.service.get_batch("fund-1", "county-tiny")["frozen_cny"], "0.00")

    def test_role_visibility_is_scoped(self) -> None:
        self.register_project("proj-east")
        self.register_project("proj-west", household="household-west")
        self.service.confirm_project("clerk-1", "proj-east")
        own = self.service.get_project("household-east", "proj-east")
        self.assertEqual(own["household_id"], "household-east")
        self.assertNotIn("created_by", own)
        self.assertNotIn("proposed_by", own["allocation"])
        with self.assertRaises(Forbidden):
            self.service.get_project("household-east", "proj-west")
        with self.assertRaises(Forbidden):
            self.service.get_batch("household-east", "central-2026")
        with self.assertRaises(Forbidden):
            self.service.list_batches("household-east")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("household-east")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("clerk-1")
        with self.assertRaises(Forbidden):
            self.service.register_batch("clerk-1", {
                "batch_id": "bad", "source": "central", "name": "越权", "total_cny": "100",
                "household_types": ["general"], "scopes": ["rebuild"], "valid_from": "2026-01-01",
                "valid_to": "2026-12-31", "per_household_cap_cny": "10", "priority": 10,
            })
        auditor_view = self.service.get_project("audit-1", "proj-east")
        self.assertIn("created_by", auditor_view)
        self.assertTrue(self.service.audit_chain("audit-1")["valid"])
        self.assertEqual(len(self.service.list_batches("fund-1")["batches"]), 3)

    def test_audit_chain_detects_tampering(self) -> None:
        self.register_project()
        self.service.confirm_project("clerk-1", "proj-1")
        self.assertTrue(self.service.audit_chain("audit-1")["valid"])
        self.connection.execute("UPDATE funding_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit-1")["valid"])

    def test_missing_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_project("clerk-1", "proj-missing")
        with self.assertRaises(NotFound):
            self.service.get_batch("fund-1", "batch-missing")
        with self.assertRaises(NotFound):
            self.service.confirm_project("clerk-1", "proj-missing")


class FundingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = FundingService(
            self.connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        )
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_actor_boundary(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("POST", "/batches", body=b"{}")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("GET", "/batches", {"X-Actor-Id": "nobody"})
        self.assertEqual(response.status, 404)
        response = self.app.handle("POST", "/unknown", {"X-Actor-Id": "clerk-1"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")

    def test_user_and_batch_routes(self) -> None:
        response = self.app.handle("POST", "/users", body=json.dumps(
            {"user_id": "fund-1", "display_name": "资金管理", "role": "fund_manager"}
        ).encode())
        self.assertEqual(response.status, 201)
        response = self.app.handle("POST", "/batches", {"X-Actor-Id": "fund-1"}, json.dumps({
            "batch_id": "central-2026", "source": "central", "name": "中央补助",
            "total_cny": "500000", "household_types": ["general"], "scopes": ["rebuild"],
            "valid_from": "2026-01-01", "valid_to": "2026-12-31",
            "per_household_cap_cny": "20000", "priority": 10,
        }).encode())
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["available_cny"], "500000.00")
        response = self.app.handle("GET", "/batches/central-2026", {"X-Actor-Id": "fund-1"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["source"], "central")


class FundingAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = acceptance_run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["confirmed_public_cny"], "45000.00")
        self.assertEqual(result["confirmed_household_cny"], "7000.00")
        self.assertEqual(result["settlement_outcome"], "completed_partial")
        self.assertTrue(result["settlement_replayed"])
        self.assertEqual(result["carried_forward_cny"], "6000.00")
        self.assertEqual(result["adjusted_kind"], "manual")
        self.assertEqual(result["household_view_lines"], 3)
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
