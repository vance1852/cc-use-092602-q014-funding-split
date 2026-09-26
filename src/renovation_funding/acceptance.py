"""贯通资金批次、规则版本、确认冻结、核销释放与结转的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict, Forbidden
from .service import FundingService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = FundingService(connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
    service.create_user("clerk", "经办员", "clerk")
    service.create_user("funds-a", "资金管理甲", "fund_manager")
    service.create_user("funds-b", "资金管理乙", "fund_manager")
    service.create_user("audit", "审计员", "auditor")
    service.register_household("clerk", {"household_id": "h-001", "head_name": "张三", "household_type": "dibao", "village": "北部村"})
    service.register_household("clerk", {"household_id": "h-002", "head_name": "李四", "household_type": "general", "village": "东部村"})
    service.create_user("family-001", "张三家属", "family", "h-001")
    batches = [
        {"batch_id": "central-2026", "source": "central", "title": "中央危房改造补助2026", "total_amount": "500000", "household_types": ["dibao", "tekun", "tuopin"], "scopes": ["rebuild", "reinforce"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "30000"},
        {"batch_id": "province-2026", "source": "provincial", "title": "省级配套资金2026", "total_amount": "300000", "household_types": ["dibao", "tekun", "tuopin", "general"], "scopes": ["rebuild", "reinforce", "repair"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "20000"},
        {"batch_id": "county-2026-a", "source": "county", "title": "县级资金甲批", "total_amount": "100000", "household_types": ["dibao", "tekun"], "scopes": ["rebuild"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "10000"},
        {"batch_id": "county-2026-b", "source": "county", "title": "县级资金乙批", "total_amount": "50000", "household_types": ["dibao", "tekun"], "scopes": ["rebuild"], "valid_from": "2026-01-01", "valid_to": "2026-12-31", "per_household_cap": "10000"},
        {"batch_id": "county-2025", "source": "county", "title": "县级资金2025", "total_amount": "40000", "household_types": ["dibao", "tekun"], "scopes": ["rebuild"], "valid_from": "2025-01-01", "valid_to": "2025-12-31", "per_household_cap": "10000"},
    ]
    for batch in batches:
        service.register_batch("funds-a", batch)
    service.create_rule("funds-a", {"rule_id": "funding-rule", "source_order": ["central", "provincial", "county"], "source_caps": {"central": "50", "provincial": "30", "county": "20"}, "household_residual": True, "note": "2026年危房改造联合资金分摊"})
    service.activate_rule("funds-a", "funding-rule", 1)
    service.register_project("clerk", {"project_id": "p-001", "household_id": "h-001", "scope": "rebuild", "estimated_cost": "65000", "address": "北部村东头12号", "idempotency_key": "proj-key-001"})
    confirmed = service.confirm_project("funds-a", "p-001", {"rule_id": "funding-rule", "idempotency_key": "confirm-001"})
    # 规则修订生成第二版并启用，已确认的 p-001 仍锁定第一版。
    service.revise_rule("funds-a", "funding-rule", {"source_caps": {"central": "60", "provincial": "30", "county": "20"}, "note": "中央补助占比上调"})
    service.activate_rule("funds-a", "funding-rule", 2)
    # 县级甲批被收回：人工调整把甲批 10000 移到乙批，乙批原 3000 转家庭自筹，双人复核形成第二版分摊。
    service.propose_adjustment("funds-a", "p-001", {"adjustment_id": "adj-001", "reason": "县级甲批资金被收回，乙批顶替，缺口转家庭自筹", "lines": [
        {"batch_id": "central-2026", "amount": "30000"},
        {"batch_id": "province-2026", "amount": "19500"},
        {"batch_id": "county-2026-b", "amount": "10000"},
        {"batch_id": None, "amount": "5500"},
    ]})
    dual_review_blocked = False
    try:
        service.review_adjustment("funds-a", "adj-001", {"decision": "approve"})
    except Forbidden:
        dual_review_blocked = True
    adjusted = service.review_adjustment("funds-b", "adj-001", {"decision": "approve"})
    # 部分完成：按实际验收量 50000 核销，其余释放；重放稳定，换内容复用编号报冲突。
    settle_payload = {"outcome": "partial", "accepted_amount": "50000", "idempotency_key": "settle-p-001"}
    settled = service.settle_project("funds-a", "p-001", settle_payload)
    replay_stable = service.settle_project("funds-a", "p-001", settle_payload) == settled
    conflict_detected = False
    try:
        service.settle_project("funds-a", "p-001", {"outcome": "partial", "accepted_amount": "51000", "idempotency_key": "settle-p-001"})
    except Conflict:
        conflict_detected = True
    # 一般户修缮项目：政府来源按户别不适用，取消后全部释放。
    service.register_project("clerk", {"project_id": "p-002", "household_id": "h-002", "scope": "repair", "estimated_cost": "20000", "address": "东部村西街3号", "idempotency_key": "proj-key-002"})
    service.confirm_project("funds-a", "p-002", {"rule_id": "funding-rule", "idempotency_key": "confirm-002"})
    cancelled = service.settle_project("funds-a", "p-002", {"outcome": "cancelled", "accepted_amount": "0", "idempotency_key": "settle-p-002"})
    # 到期批次余额结转至新批次。
    carried = service.carry_forward_batch("funds-a", "county-2025", {"new_batch_id": "county-2026-c", "valid_from": "2026-10-01", "valid_to": "2027-03-31", "idempotency_key": "carry-001"})
    # 家庭角色只能看到本户授权明细。
    family_view = service.get_project("family-001", "p-001")
    family_blocked = False
    try:
        service.get_project("family-001", "p-002")
    except Forbidden:
        family_blocked = True
    conservation_ok = all(
        Decimal(row["total_amount"])
        == Decimal(row["available_amount"]) + Decimal(row["frozen_amount"])
        + Decimal(row["spent_amount"]) + Decimal(row["carried_out"])
        for row in connection.execute("SELECT * FROM fund_batches").fetchall()
    )
    result = {
        "status": "ok",
        "allocation_lines": [
            {"source": line["source"], "amount": line["amount"]} for line in confirmed["lines"]
        ],
        "rule_version_locked": service.get_project("audit", "p-001")["rule_version"],
        "adjusted_version": adjusted["allocation_version"],
        "dual_review_blocked": dual_review_blocked,
        "settled": {"state": settled["state"], "written_total": settled["written_total"], "released_total": settled["released_total"]},
        "replay_stable": replay_stable,
        "conflict_detected": conflict_detected,
        "cancelled": {"state": cancelled["state"], "released_total": cancelled["released_total"]},
        "carried": carried,
        "family_view_sources": [line["source"] for line in family_view["allocation"]["lines"]],
        "family_view_hides_batch": all("batch_id" not in line for line in family_view["allocation"]["lines"]),
        "family_blocked": family_blocked,
        "conservation_ok": conservation_ok,
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行危房改造联合资金分摊离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
