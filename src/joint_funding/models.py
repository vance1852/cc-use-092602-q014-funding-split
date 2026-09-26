"""危房改造联合资金分摊领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
FUND_SOURCES = {"central", "provincial", "county"}
HOUSEHOLD_TYPES = {"minimum-living", "extreme-hardship", "poverty-lifted", "general"}
RENOVATION_SCOPES = {"rebuild", "reinforce", "partial-repair"}
REMAINDER_POLICIES = {"release", "carry_forward"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def _sorted_terms(value: object, field: str, allowed: set[str]) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    terms: list[str] = []
    for item in value:
        term = required_text(item, field, 32)
        if term not in allowed:
            raise ValidationFailed(f"{field} 包含不支持的取值: {term}")
        if term in terms:
            raise ValidationFailed(f"{field} 存在重复取值: {term}")
        terms.append(term)
    return sorted(terms)


@dataclass(frozen=True, slots=True)
class FundBatchInput:
    """资金批次：来源、适用户别、改造范围、有效期与每户封顶。"""

    batch_id: str
    source: str
    name: str
    total_cny: Decimal
    household_types: list[str]
    scopes: list[str]
    valid_from: str
    valid_to: str
    per_household_cap_cny: Decimal
    priority: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FundBatchInput":
        source = required_text(raw.get("source"), "source", 16)
        if source not in FUND_SOURCES:
            raise ValidationFailed("source 必须是 central、provincial 或 county")
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_to = date_text(raw.get("valid_to"), "valid_to")
        if valid_to < valid_from:
            raise ValidationFailed("valid_to 不能早于 valid_from")
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        total = decimal_value(raw.get("total_cny"), "total_cny", minimum=Decimal("0.01"))
        cap = decimal_value(raw.get("per_household_cap_cny"), "per_household_cap_cny", minimum=Decimal("0.01"))
        if cap > total:
            raise ValidationFailed("per_household_cap_cny 不能大于 total_cny")
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            source=source,
            name=required_text(raw.get("name"), "name"),
            total_cny=total,
            household_types=_sorted_terms(raw.get("household_types"), "household_types", HOUSEHOLD_TYPES),
            scopes=_sorted_terms(raw.get("scopes"), "scopes", RENOVATION_SCOPES),
            valid_from=valid_from,
            valid_to=valid_to,
            per_household_cap_cny=cap,
            priority=priority,
        )


@dataclass(frozen=True, slots=True)
class AllocationRuleInput:
    """分摊规则：按来源排序的分摊步骤，修订形成新版本。"""

    rule_id: str
    name: str
    steps: list[str]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AllocationRuleInput":
        steps = raw.get("steps")
        if not isinstance(steps, Sequence) or isinstance(steps, str) or not steps:
            raise ValidationFailed("steps 必须是非空数组")
        parsed: list[str] = []
        for item in steps:
            step = required_text(item, "steps", 16)
            if step not in FUND_SOURCES:
                raise ValidationFailed(f"steps 包含不支持的来源: {step}")
            if step in parsed:
                raise ValidationFailed(f"steps 存在重复来源: {step}")
            parsed.append(step)
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            name=required_text(raw.get("name"), "name"),
            steps=parsed,
        )


@dataclass(frozen=True, slots=True)
class ProjectInput:
    """改造项目：户别、改造范围与预估造价。"""

    project_id: str
    household_id: str
    household_type: str
    scope: str
    estimated_cost_cny: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProjectInput":
        household_type = required_text(raw.get("household_type"), "household_type", 32)
        if household_type not in HOUSEHOLD_TYPES:
            raise ValidationFailed("household_type 不是受支持的户别")
        scope = required_text(raw.get("scope"), "scope", 32)
        if scope not in RENOVATION_SCOPES:
            raise ValidationFailed("scope 不是受支持的改造范围")
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            household_type=household_type,
            scope=scope,
            estimated_cost_cny=decimal_value(
                raw.get("estimated_cost_cny"), "estimated_cost_cny", minimum=Decimal("0.01")
            ),
        )


@dataclass(frozen=True, slots=True)
class SettlementInput:
    """完工核销：按实际验收造价核销，余量释放或结转。"""

    settlement_id: str
    project_id: str
    actual_cost_cny: Decimal
    remainder_policy: str
    carry_forward_to: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SettlementInput":
        policy = required_text(raw.get("remainder_policy", "release"), "remainder_policy", 16)
        if policy not in REMAINDER_POLICIES:
            raise ValidationFailed("remainder_policy 必须是 release 或 carry_forward")
        target = raw.get("carry_forward_to")
        if policy == "carry_forward":
            carry_to = identifier(target, "carry_forward_to")
        else:
            if target is not None:
                raise ValidationFailed("remainder_policy 为 release 时不能指定 carry_forward_to")
            carry_to = None
        return cls(
            settlement_id=identifier(raw.get("settlement_id"), "settlement_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            actual_cost_cny=decimal_value(
                raw.get("actual_cost_cny"), "actual_cost_cny", minimum=Decimal("0")
            ),
            remainder_policy=policy,
            carry_forward_to=carry_to,
        )


@dataclass(frozen=True, slots=True)
class AdjustmentLine:
    batch_id: str
    amount_cny: Decimal


@dataclass(frozen=True, slots=True)
class AdjustmentInput:
    """人工调整：提议新的批次分摊明细，须双人复核。"""

    adjustment_id: str
    project_id: str
    lines: list[AdjustmentLine]
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AdjustmentInput":
        lines_raw = raw.get("lines")
        if not isinstance(lines_raw, Sequence) or isinstance(lines_raw, str) or not lines_raw:
            raise ValidationFailed("lines 必须是非空数组")
        lines: list[AdjustmentLine] = []
        seen: set[str] = set()
        for item in lines_raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("lines 元素必须是对象")
            batch_id = identifier(item.get("batch_id"), "lines.batch_id")
            if batch_id in seen:
                raise ValidationFailed(f"lines 存在重复批次: {batch_id}")
            seen.add(batch_id)
            lines.append(AdjustmentLine(
                batch_id=batch_id,
                amount_cny=decimal_value(item.get("amount_cny"), "lines.amount_cny", minimum=Decimal("0.01")),
            ))
        return cls(
            adjustment_id=identifier(raw.get("adjustment_id"), "adjustment_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            lines=sorted(lines, key=lambda line: line.batch_id),
            reason=required_text(raw.get("reason"), "reason"),
        )
