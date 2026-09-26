"""贯通资金批次、规则版本、项目确认、核销结转和双人复核的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import FundingService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = FundingService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (
        ("clerk-1", "clerk"),
        ("fund-1", "fund_manager"),
        ("fund-2", "fund_manager"),
        ("household-east", "household"),
        ("audit-1", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    service.register_batch("fund-1", {
        "batch_id": "central-2026", "source": "central", "name": "2026 年中央危房改造补助",
        "total_cny": "500000", "household_types": ["minimum-living", "extreme-hardship"],
        "scopes": ["rebuild", "reinforce"], "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "per_household_cap_cny": "20000", "priority": 10,
    })
    service.register_batch("fund-1", {
        "batch_id": "province-2026", "source": "provincial", "name": "2026 年省级配套资金",
        "total_cny": "300000", "household_types": ["minimum-living", "extreme-hardship", "poverty-lifted"],
        "scopes": ["rebuild", "reinforce"], "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "per_household_cap_cny": "15000", "priority": 20,
    })
    service.register_batch("fund-1", {
        "batch_id": "county-2026", "source": "county", "name": "2026 年县级危房改造资金",
        "total_cny": "200000", "household_types": ["minimum-living", "extreme-hardship", "poverty-lifted", "general"],
        "scopes": ["rebuild", "reinforce", "partial-repair"], "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "per_household_cap_cny": "10000", "priority": 30,
    })
    service.register_batch("fund-1", {
        "batch_id": "county-2027", "source": "county", "name": "2027 年县级结转资金",
        "total_cny": "1000", "household_types": ["general"], "scopes": ["partial-repair"],
        "valid_from": "2026-01-01", "valid_to": "2027-12-31",
        "per_household_cap_cny": "1000", "priority": 40,
    })
    service.register_rule("fund-1", {
        "rule_id": "joint-funding", "name": "中央、省、县依次分摊，余量家庭自筹",
        "steps": ["central", "provincial", "county"],
    })
    service.register_project("clerk-1", {
        "project_id": "proj-001", "household_id": "household-east",
        "household_type": "minimum-living", "scope": "rebuild", "estimated_cost_cny": "52000",
    })
    confirmed = service.confirm_project("clerk-1", "proj-001")
    settlement = service.settle_project("clerk-1", {
        "settlement_id": "settle-001", "project_id": "proj-001", "actual_cost_cny": "39000",
        "remainder_policy": "carry_forward", "carry_forward_to": "county-2027",
    })
    replayed = service.settle_project("clerk-1", {
        "settlement_id": "settle-001", "project_id": "proj-001", "actual_cost_cny": "39000",
        "remainder_policy": "carry_forward", "carry_forward_to": "county-2027",
    })
    service.register_project("clerk-1", {
        "project_id": "proj-002", "household_id": "household-east",
        "household_type": "minimum-living", "scope": "reinforce", "estimated_cost_cny": "30000",
    })
    service.confirm_project("clerk-1", "proj-002")
    service.propose_adjustment("fund-1", {
        "adjustment_id": "adj-001", "project_id": "proj-002",
        "lines": [
            {"batch_id": "central-2026", "amount_cny": "18000"},
            {"batch_id": "county-2026", "amount_cny": "6000"},
        ],
        "reason": "省级批次额度紧张，改由中央与县级承担",
    })
    adjusted = service.approve_adjustment("fund-2", "adj-001")
    household_view = service.get_project("household-east", "proj-001")
    result = {
        "status": "ok",
        "confirmed_public_cny": confirmed["allocation"]["public_total_cny"],
        "confirmed_household_cny": confirmed["allocation"]["household_share_cny"],
        "settlement_outcome": settlement["outcome"],
        "settlement_replayed": replayed["replayed"],
        "carried_forward_cny": settlement["remainders"][0]["remainder_cny"],
        "adjusted_kind": adjusted["allocation"]["kind"],
        "household_view_lines": len(household_view["allocation"]["lines"]),
        "audit": service.audit_chain("audit-1"),
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
