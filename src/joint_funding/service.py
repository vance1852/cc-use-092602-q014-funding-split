"""危房改造联合资金分摊的事务用例。

资金批次登记、规则版本化、项目确认冻结、完工核销、取消/失败释放、
人工调整双人复核和哈希链审计都在单个 SQLite 连接上完成。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    AdjustmentInput,
    AllocationRuleInput,
    FundBatchInput,
    ProjectInput,
    SettlementInput,
)
from .sharing import (
    BatchSlice,
    allocate_cost,
    canonical_json,
    decimal_text,
    digest,
    quantize_money,
    settle_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "clerk": {"project.write", "project.read"},
    "fund_manager": {
        "batch.write", "rule.write", "adjust.propose", "adjust.review",
        "fund.read", "project.read",
    },
    "household": {"own.read"},
    "auditor": {"audit.read", "project.read", "fund.read"},
}

ZERO = Decimal("0")


class FundingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM funding_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM funding_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO funding_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO funding_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 资金批次
    # ------------------------------------------------------------------
    def register_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "batch.write")
        batch = FundBatchInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO fund_batches(batch_id,source,name,total_cny,household_types_json,"
                    "scopes_json,valid_from,valid_to,per_household_cap_cny,priority,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch.batch_id,
                        batch.source,
                        batch.name,
                        decimal_text(quantize_money(batch.total_cny)),
                        canonical_json(batch.household_types),
                        canonical_json(batch.scopes),
                        batch.valid_from,
                        batch.valid_to,
                        decimal_text(quantize_money(batch.per_household_cap_cny)),
                        batch.priority,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("fund_batch", batch.batch_id, "batch.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("资金批次编号已经存在") from exc
        return self.get_batch(actor_id, batch.batch_id)

    def _batch_row(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM fund_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资金批次不存在")
        return row

    @staticmethod
    def _available(row: sqlite3.Row) -> Decimal:
        return quantize_money(
            Decimal(row["total_cny"]) - Decimal(row["frozen_cny"]) - Decimal(row["used_cny"])
        )

    def _batch_view(self, row: sqlite3.Row) -> dict[str, Any]:
        view = {
            "batch_id": row["batch_id"],
            "source": row["source"],
            "name": row["name"],
            "total_cny": row["total_cny"],
            "frozen_cny": row["frozen_cny"],
            "used_cny": row["used_cny"],
            "available_cny": decimal_text(self._available(row)),
            "household_types": json.loads(row["household_types_json"]),
            "scopes": json.loads(row["scopes_json"]),
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "per_household_cap_cny": row["per_household_cap_cny"],
            "priority": row["priority"],
            "state": row["state"],
            "revision": row["revision"],
        }
        transfers = self.connection.execute(
            "SELECT from_batch_id,to_batch_id,amount_cny,reason,project_id,settlement_id,created_at "
            "FROM fund_transfers WHERE from_batch_id=? OR to_batch_id=? ORDER BY transfer_id",
            (row["batch_id"], row["batch_id"]),
        ).fetchall()
        view["transfers"] = [dict(item) for item in transfers]
        return view

    def get_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "fund.read")
        return self._batch_view(self._batch_row(batch_id))

    def list_batches(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "fund.read")
        rows = self.connection.execute(
            "SELECT * FROM fund_batches ORDER BY source,priority,valid_from,batch_id"
        ).fetchall()
        return {"batches": [self._batch_view(row) for row in rows]}

    def close_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "batch.write")
        row = self._batch_row(batch_id)
        if row["state"] != "active":
            raise InvalidState("资金批次已关闭")
        if Decimal(row["frozen_cny"]) > ZERO:
            raise InvalidState("批次仍存在冻结额度，不能关闭")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE fund_batches SET state='closed',revision=revision+1 "
                "WHERE batch_id=? AND state='active' AND frozen_cny='0.00'",
                (batch_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("资金批次当前不可关闭")
            self._audit("fund_batch", batch_id, "batch.closed", actor_id, {})
        return self.get_batch(actor_id, batch_id)

    # ------------------------------------------------------------------
    # 分摊规则（版本化，修订不回写已确认项目）
    # ------------------------------------------------------------------
    def register_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        rule = AllocationRuleInput.from_dict(raw)
        definition = {"rule_id": rule.rule_id, "name": rule.name, "steps": rule.steps}
        content_sha256 = digest(definition)
        previous = self.connection.execute(
            "SELECT max(version) AS max_version FROM allocation_rules WHERE rule_id=?",
            (rule.rule_id,),
        ).fetchone()
        version = 1 if previous["max_version"] is None else int(previous["max_version"]) + 1
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE allocation_rules SET state='superseded' WHERE rule_id=? AND state='active'",
                    (rule.rule_id,),
                )
                self.connection.execute(
                    "INSERT INTO allocation_rules(rule_id,version,name,definition_json,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (rule.rule_id, version, rule.name, canonical_json(definition), content_sha256, actor_id, self._now()),
                )
                self._audit(
                    "allocation_rule", rule.rule_id, "rule.registered", actor_id,
                    {"version": version, "sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("相同内容的规则版本已经登记") from exc
        return {"rule_id": rule.rule_id, "version": version, "state": "active", "sha256": content_sha256}

    def get_rule(self, actor_id: str, rule_id: str, version: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "fund.read")
        if version is None:
            row = self.connection.execute(
                "SELECT * FROM allocation_rules WHERE rule_id=? ORDER BY version DESC LIMIT 1",
                (rule_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM allocation_rules WHERE rule_id=? AND version=?",
                (rule_id, version),
            ).fetchone()
        if row is None:
            raise NotFound("分摊规则不存在")
        return {
            "rule_id": row["rule_id"],
            "version": row["version"],
            "state": row["state"],
            "definition": json.loads(row["definition_json"]),
            "sha256": row["content_sha256"],
        }

    def _active_rule(self, rule_id: str | None) -> sqlite3.Row:
        if rule_id is None:
            rows = self.connection.execute(
                "SELECT DISTINCT rule_id FROM allocation_rules WHERE state='active'"
            ).fetchall()
            if not rows:
                raise InvalidState("没有可用的分摊规则")
            if len(rows) > 1:
                raise ValidationFailed("存在多条有效分摊规则，必须指定 rule_id")
            rule_id = rows[0]["rule_id"]
        row = self.connection.execute(
            "SELECT * FROM allocation_rules WHERE rule_id=? AND state='active' "
            "ORDER BY version DESC LIMIT 1",
            (rule_id,),
        ).fetchone()
        if row is None:
            raise NotFound("分摊规则不存在或已停用")
        return row

    # ------------------------------------------------------------------
    # 改造项目
    # ------------------------------------------------------------------
    def register_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        project = ProjectInput.from_dict(raw)
        household = self._user(project.household_id)
        if household["role"] != "household":
            raise ValidationFailed("household_id 必须是在册的家庭用户")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO renovation_projects(project_id,household_id,household_type,scope,"
                    "estimated_cost_cny,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        project.project_id,
                        project.household_id,
                        project.household_type,
                        project.scope,
                        decimal_text(quantize_money(project.estimated_cost_cny)),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("project", project.project_id, "project.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("改造项目编号已经存在") from exc
        return self.get_project(actor_id, project.project_id)

    def _project_row(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM renovation_projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound("改造项目不存在")
        return row

    def _active_allocation(self, project: sqlite3.Row) -> sqlite3.Row | None:
        if project["active_allocation_version"] is None:
            return None
        return self.connection.execute(
            "SELECT * FROM project_allocations WHERE project_id=? AND version=?",
            (project["project_id"], int(project["active_allocation_version"])),
        ).fetchone()

    def get_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        project = self._project_row(project_id)
        if actor["role"] == "household":
            if project["household_id"] != actor_id:
                raise Forbidden("家庭只能查看本户项目明细")
        elif "project.read" not in ROLE_PERMISSIONS[actor["role"]]:
            raise Forbidden(f"角色 {actor['role']} 无权查看项目")
        view: dict[str, Any] = {
            "project_id": project["project_id"],
            "household_id": project["household_id"],
            "household_type": project["household_type"],
            "scope": project["scope"],
            "estimated_cost_cny": project["estimated_cost_cny"],
            "state": project["state"],
            "rule_id": project["rule_id"],
            "rule_version": project["rule_version"],
            "failure_reason": project["failure_reason"],
            "created_at": project["created_at"],
            "confirmed_at": project["confirmed_at"],
            "closed_at": project["closed_at"],
        }
        allocation = self._active_allocation(project)
        if allocation is not None:
            view["allocation"] = {
                "version": allocation["version"],
                "kind": allocation["kind"],
                "rule_version": allocation["rule_version"],
                "lines": json.loads(allocation["lines_json"]),
                "public_total_cny": allocation["public_total_cny"],
                "household_share_cny": allocation["household_share_cny"],
                "input_sha256": allocation["input_sha256"],
            }
            if actor["role"] != "household":
                view["allocation"]["proposed_by"] = allocation["proposed_by"]
                view["allocation"]["approved_by"] = allocation["approved_by"]
        else:
            view["allocation"] = None
        settlements = self.connection.execute(
            "SELECT settlement_id,actual_cost_cny,outcome,remainder_policy,created_at "
            "FROM settlements WHERE project_id=? ORDER BY created_at,settlement_id",
            (project_id,),
        ).fetchall()
        view["settlements"] = [dict(item) for item in settlements]
        if actor["role"] != "household":
            view["created_by"] = project["created_by"]
        return view

    def _eligible_batches(self, household_type: str, scope: str, on_date: str) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM fund_batches WHERE state='active' AND valid_from<=? AND valid_to>=? "
            "ORDER BY priority,valid_from,batch_id",
            (on_date, on_date),
        ).fetchall()
        eligible = []
        for row in rows:
            if household_type not in json.loads(row["household_types_json"]):
                continue
            if scope not in json.loads(row["scopes_json"]):
                continue
            if self._available(row) <= ZERO:
                continue
            eligible.append(row)
        return eligible

    def _freeze(self, batch_id: str, amount: Decimal) -> None:
        row = self._batch_row(batch_id)
        if row["state"] != "active":
            raise InvalidState(f"资金批次 {batch_id} 已关闭")
        available = self._available(row)
        if available < amount:
            raise Conflict(f"资金批次 {batch_id} 可用余额不足")
        self.connection.execute(
            "UPDATE fund_batches SET frozen_cny=?,revision=revision+1 WHERE batch_id=?",
            (decimal_text(quantize_money(Decimal(row["frozen_cny"]) + amount)), batch_id),
        )

    def _release(self, batch_id: str, amount: Decimal) -> None:
        if amount <= ZERO:
            return
        row = self._batch_row(batch_id)
        frozen = Decimal(row["frozen_cny"])
        if frozen < amount:
            raise InvalidState(f"资金批次 {batch_id} 冻结额度不足")
        self.connection.execute(
            "UPDATE fund_batches SET frozen_cny=?,revision=revision+1 WHERE batch_id=?",
            (decimal_text(quantize_money(frozen - amount)), batch_id),
        )

    def _write_off(self, batch_id: str, amount: Decimal) -> None:
        if amount <= ZERO:
            return
        row = self._batch_row(batch_id)
        frozen = Decimal(row["frozen_cny"])
        if frozen < amount:
            raise InvalidState(f"资金批次 {batch_id} 冻结额度不足")
        self.connection.execute(
            "UPDATE fund_batches SET frozen_cny=?,used_cny=?,revision=revision+1 WHERE batch_id=?",
            (
                decimal_text(quantize_money(frozen - amount)),
                decimal_text(quantize_money(Decimal(row["used_cny"]) + amount)),
                batch_id,
            ),
        )

    def _carry_forward(
        self,
        from_batch_id: str,
        to_batch_id: str,
        amount: Decimal,
        project_id: str,
        settlement_id: str,
        actor_id: str,
    ) -> None:
        if amount <= ZERO:
            return
        source = self._batch_row(from_batch_id)
        target = self._batch_row(to_batch_id)
        if target["state"] != "active":
            raise InvalidState(f"结转目标批次 {to_batch_id} 不可用")
        frozen = Decimal(source["frozen_cny"])
        if frozen < amount:
            raise InvalidState(f"资金批次 {from_batch_id} 冻结额度不足")
        now = self._now()
        self.connection.execute(
            "UPDATE fund_batches SET total_cny=?,frozen_cny=?,revision=revision+1 WHERE batch_id=?",
            (
                decimal_text(quantize_money(Decimal(source["total_cny"]) - amount)),
                decimal_text(quantize_money(frozen - amount)),
                from_batch_id,
            ),
        )
        self.connection.execute(
            "UPDATE fund_batches SET total_cny=?,revision=revision+1 WHERE batch_id=?",
            (decimal_text(quantize_money(Decimal(target["total_cny"]) + amount)), to_batch_id),
        )
        self.connection.execute(
            "INSERT INTO fund_transfers(from_batch_id,to_batch_id,amount_cny,reason,project_id,"
            "settlement_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (from_batch_id, to_batch_id, decimal_text(quantize_money(amount)),
             "完工核销余量结转", project_id, settlement_id, actor_id, now),
        )

    def confirm_project(self, actor_id: str, project_id: str, rule_id: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        project = self._project_row(project_id)
        if project["state"] != "draft":
            raise InvalidState("项目不是草稿状态，不能确认")
        rule = self._active_rule(rule_id)
        definition = json.loads(rule["definition_json"])
        on_date = self._now()[:10]
        batches = self._eligible_batches(project["household_type"], project["scope"], on_date)
        slices = [
            BatchSlice(
                batch_id=row["batch_id"],
                source=row["source"],
                cap_cny=Decimal(row["per_household_cap_cny"]),
                available_cny=self._available(row),
                priority=int(row["priority"]),
                valid_from=row["valid_from"],
            )
            for row in batches
        ]
        allocation = allocate_cost(Decimal(project["estimated_cost_cny"]), definition["steps"], slices)
        input_sha256 = digest({
            "project_id": project_id,
            "estimated_cost_cny": project["estimated_cost_cny"],
            "rule_sha256": rule["content_sha256"],
            "batches": [
                {"batch_id": row["batch_id"], "available_cny": decimal_text(self._available(row))}
                for row in batches
            ],
        })
        now = self._now()
        with transaction(self.connection, immediate=True):
            for line in allocation["lines"]:
                self._freeze(str(line["batch_id"]), Decimal(str(line["amount_cny"])))
            self.connection.execute(
                "INSERT INTO project_allocations(project_id,version,kind,rule_id,rule_version,lines_json,"
                "input_sha256,public_total_cny,household_share_cny,proposed_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    1,
                    "rule",
                    rule["rule_id"],
                    rule["version"],
                    canonical_json(allocation["lines"]),
                    input_sha256,
                    allocation["public_total_cny"],
                    allocation["household_share_cny"],
                    actor_id,
                    now,
                ),
            )
            cursor = self.connection.execute(
                "UPDATE renovation_projects SET state='confirmed',rule_id=?,rule_version=?,"
                "active_allocation_version=1,confirmed_at=?,revision=revision+1 "
                "WHERE project_id=? AND state='draft'",
                (rule["rule_id"], rule["version"], now, project_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("项目不是草稿状态，不能确认")
            self._audit("project", project_id, "project.confirmed", actor_id, {
                "rule_id": rule["rule_id"],
                "rule_version": rule["version"],
                "input_sha256": input_sha256,
                "public_total_cny": allocation["public_total_cny"],
                "household_share_cny": allocation["household_share_cny"],
            })
        return self.get_project(actor_id, project_id)

    def _release_allocation(self, allocation: sqlite3.Row) -> None:
        for line in json.loads(allocation["lines_json"]):
            self._release(str(line["batch_id"]), Decimal(str(line["amount_cny"])))

    def cancel_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        project = self._project_row(project_id)
        if project["state"] not in {"draft", "confirmed"}:
            raise InvalidState("项目当前状态不能取消")
        now = self._now()
        with transaction(self.connection, immediate=True):
            allocation = self._active_allocation(project)
            if allocation is not None:
                self._release_allocation(allocation)
            cursor = self.connection.execute(
                "UPDATE renovation_projects SET state='cancelled',closed_at=?,revision=revision+1 "
                "WHERE project_id=? AND state IN ('draft','confirmed')",
                (now, project_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("项目当前状态不能取消")
            self._audit("project", project_id, "project.cancelled", actor_id, {})
        return self.get_project(actor_id, project_id)

    def fail_project(self, actor_id: str, project_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("失败原因不能为空")
        project = self._project_row(project_id)
        if project["state"] != "confirmed":
            raise InvalidState("只有已确认项目可以登记失败")
        now = self._now()
        with transaction(self.connection, immediate=True):
            allocation = self._active_allocation(project)
            if allocation is not None:
                self._release_allocation(allocation)
            cursor = self.connection.execute(
                "UPDATE renovation_projects SET state='failed',failure_reason=?,closed_at=?,"
                "revision=revision+1 WHERE project_id=? AND state='confirmed'",
                (reason.strip(), now, project_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有已确认项目可以登记失败")
            self._audit("project", project_id, "project.failed", actor_id, {"reason": reason.strip()})
        return self.get_project(actor_id, project_id)

    # ------------------------------------------------------------------
    # 完工核销（幂等）
    # ------------------------------------------------------------------
    def settle_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        settlement = SettlementInput.from_dict(raw)
        request_sha256 = digest({
            "settlement_id": settlement.settlement_id,
            "project_id": settlement.project_id,
            "actual_cost_cny": decimal_text(quantize_money(settlement.actual_cost_cny)),
            "remainder_policy": settlement.remainder_policy,
            "carry_forward_to": settlement.carry_forward_to,
        })
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM settlements WHERE settlement_id=?",
            (settlement.settlement_id,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_sha256:
                raise Conflict("核销编号对应不同的核销内容")
            return {**json.loads(stored["response_json"]), "replayed": True}
        project = self._project_row(settlement.project_id)
        if project["state"] != "confirmed":
            raise InvalidState("只有已确认项目可以完工核销")
        allocation = self._active_allocation(project)
        if allocation is None:
            raise InvalidState("项目缺少已冻结分摊")
        rule = self.connection.execute(
            "SELECT * FROM allocation_rules WHERE rule_id=? AND version=?",
            (allocation["rule_id"], allocation["rule_version"]),
        ).fetchone()
        if rule is None:
            raise InvalidState("项目引用的分摊规则版本不存在")
        steps = json.loads(rule["definition_json"])["steps"]
        frozen_lines = json.loads(allocation["lines_json"])
        result = settle_cost(settlement.actual_cost_cny, steps, frozen_lines)
        estimated = Decimal(project["estimated_cost_cny"])
        actual = quantize_money(settlement.actual_cost_cny)
        outcome = "completed" if actual >= estimated else "completed_partial"
        if settlement.remainder_policy == "carry_forward":
            target = self._batch_row(settlement.carry_forward_to)
            if target["state"] != "active":
                raise InvalidState("结转目标批次不可用")
        now = self._now()
        response = {
            "settlement_id": settlement.settlement_id,
            "project_id": settlement.project_id,
            "outcome": outcome,
            "estimated_cost_cny": project["estimated_cost_cny"],
            "actual_cost_cny": decimal_text(actual),
            "public_total_cny": result["public_total_cny"],
            "household_share_cny": result["household_share_cny"],
            "write_offs": result["write_offs"],
            "remainders": result["remainders"],
            "remainder_policy": settlement.remainder_policy,
            "carry_forward_to": settlement.carry_forward_to,
        }
        with transaction(self.connection, immediate=True):
            for write_off in result["write_offs"]:
                self._write_off(str(write_off["batch_id"]), Decimal(str(write_off["written_off_cny"])))
            for remainder in result["remainders"]:
                amount = Decimal(str(remainder["remainder_cny"]))
                if settlement.remainder_policy == "release":
                    self._release(str(remainder["batch_id"]), amount)
                else:
                    self._carry_forward(
                        str(remainder["batch_id"]), settlement.carry_forward_to, amount,
                        settlement.project_id, settlement.settlement_id, actor_id,
                    )
            cursor = self.connection.execute(
                "UPDATE renovation_projects SET state=?,closed_at=?,revision=revision+1 "
                "WHERE project_id=? AND state='confirmed'",
                (outcome, now, settlement.project_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有已确认项目可以完工核销")
            self.connection.execute(
                "INSERT INTO settlements(settlement_id,project_id,request_sha256,actual_cost_cny,"
                "remainder_policy,outcome,lines_json,remainders_json,response_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    settlement.settlement_id,
                    settlement.project_id,
                    request_sha256,
                    decimal_text(actual),
                    settlement.remainder_policy,
                    outcome,
                    canonical_json(result["write_offs"]),
                    canonical_json(result["remainders"]),
                    canonical_json(response),
                    actor_id,
                    now,
                ),
            )
            self._audit("project", settlement.project_id, "project.settled", actor_id, {
                "settlement_id": settlement.settlement_id,
                "actual_cost_cny": decimal_text(actual),
                "outcome": outcome,
                "remainder_policy": settlement.remainder_policy,
            })
        return {**response, "replayed": False}

    # ------------------------------------------------------------------
    # 人工调整（双人复核形成新版本）
    # ------------------------------------------------------------------
    def propose_adjustment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "adjust.propose")
        adjustment = AdjustmentInput.from_dict(raw)
        project = self._project_row(adjustment.project_id)
        if project["state"] != "confirmed":
            raise InvalidState("只有已确认项目可以人工调整")
        total = sum((line.amount_cny for line in adjustment.lines), ZERO)
        if quantize_money(total) > Decimal(project["estimated_cost_cny"]):
            raise ValidationFailed("调整后的公共资金合计不能超过预估造价")
        for line in adjustment.lines:
            self._batch_row(line.batch_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO adjustments(adjustment_id,project_id,lines_json,reason,proposed_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        adjustment.adjustment_id,
                        adjustment.project_id,
                        canonical_json([
                            {"batch_id": line.batch_id, "amount_cny": decimal_text(quantize_money(line.amount_cny))}
                            for line in adjustment.lines
                        ]),
                        adjustment.reason,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("adjustment", adjustment.adjustment_id, "adjustment.proposed", actor_id, {
                    "project_id": adjustment.project_id,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("调整编号已经存在") from exc
        return {"adjustment_id": adjustment.adjustment_id, "state": "pending"}

    def _decide_adjustment(self, actor_id: str, adjustment_id: str) -> sqlite3.Row:
        adjustment = self.connection.execute(
            "SELECT * FROM adjustments WHERE adjustment_id=?", (adjustment_id,)
        ).fetchone()
        if adjustment is None:
            raise NotFound("人工调整不存在")
        if adjustment["state"] != "pending":
            raise InvalidState("人工调整已经复核")
        if adjustment["proposed_by"] == actor_id:
            raise Forbidden("复核人不能是提议人")
        return adjustment

    def approve_adjustment(self, actor_id: str, adjustment_id: str) -> dict[str, Any]:
        self._require(actor_id, "adjust.review")
        adjustment = self._decide_adjustment(actor_id, adjustment_id)
        project = self._project_row(adjustment["project_id"])
        if project["state"] != "confirmed":
            raise InvalidState("项目当前状态不能应用人工调整")
        current = self._active_allocation(project)
        if current is None:
            raise InvalidState("项目缺少已冻结分摊")
        new_lines_raw = json.loads(adjustment["lines_json"])
        new_lines: list[dict[str, Any]] = []
        for item in new_lines_raw:
            batch = self._batch_row(str(item["batch_id"]))
            if batch["state"] != "active":
                raise InvalidState(f"资金批次 {batch['batch_id']} 已关闭")
            amount = quantize_money(Decimal(str(item["amount_cny"])))
            new_lines.append({
                "batch_id": batch["batch_id"],
                "source": batch["source"],
                "amount_cny": decimal_text(amount),
                "demand_before_cny": decimal_text(amount),
                "per_household_cap_cny": batch["per_household_cap_cny"],
                "available_before_cny": decimal_text(self._available(batch)),
                "binding_constraint": "manual",
            })
        public_total = quantize_money(sum((Decimal(line["amount_cny"]) for line in new_lines), ZERO))
        household_share = quantize_money(Decimal(project["estimated_cost_cny"]) - public_total)
        now = self._now()
        new_version = int(current["version"]) + 1
        with transaction(self.connection, immediate=True):
            self._release_allocation(current)
            for line in new_lines:
                self._freeze(str(line["batch_id"]), Decimal(str(line["amount_cny"])))
            self.connection.execute(
                "UPDATE project_allocations SET state='superseded' WHERE project_id=? AND version=?",
                (project["project_id"], current["version"]),
            )
            self.connection.execute(
                "INSERT INTO project_allocations(project_id,version,kind,rule_id,rule_version,lines_json,"
                "input_sha256,public_total_cny,household_share_cny,proposed_by,approved_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project["project_id"],
                    new_version,
                    "manual",
                    current["rule_id"],
                    current["rule_version"],
                    canonical_json(new_lines),
                    digest({"adjustment_id": adjustment_id, "lines": new_lines_raw}),
                    decimal_text(public_total),
                    decimal_text(household_share),
                    adjustment["proposed_by"],
                    actor_id,
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE renovation_projects SET active_allocation_version=?,revision=revision+1 "
                "WHERE project_id=?",
                (new_version, project["project_id"]),
            )
            self.connection.execute(
                "UPDATE adjustments SET state='approved',decided_by=?,decided_at=? WHERE adjustment_id=?",
                (actor_id, now, adjustment_id),
            )
            self._audit("adjustment", adjustment_id, "adjustment.approved", actor_id, {
                "project_id": project["project_id"],
                "allocation_version": new_version,
            })
        return self.get_project(actor_id, project["project_id"])

    def reject_adjustment(self, actor_id: str, adjustment_id: str) -> dict[str, Any]:
        self._require(actor_id, "adjust.review")
        adjustment = self._decide_adjustment(actor_id, adjustment_id)
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE adjustments SET state='rejected',decided_by=?,decided_at=? WHERE adjustment_id=?",
                (actor_id, now, adjustment_id),
            )
            self._audit("adjustment", adjustment_id, "adjustment.rejected", actor_id, {
                "project_id": adjustment["project_id"],
            })
        return {"adjustment_id": adjustment_id, "state": "rejected"}

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM funding_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
