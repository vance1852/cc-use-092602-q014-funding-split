"""确定且可解释的联合资金分摊与核销计算。

分摊顺序由规则版本固定：政府来源按 source_order 依次承担，每个来源内部
按批次到期日（再按批次编号）排序使用；家庭自筹不登记批次，只作为规则
允许时的兜底承诺行。每一行都记录命中的是哪一条上限，保证任何一分钱
都能说明来自哪里、为什么是这个数。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .models import HOUSEHOLD_TYPE_LABELS, SCOPE_LABELS, SOURCE_LABELS


ZERO = Decimal("0")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class InsufficientFunds(ValueError):
    """政府资金批次与家庭自筹规则无法覆盖改造造价。"""


@dataclass(frozen=True, slots=True)
class BatchSpec:
    batch_id: str
    source: str
    available: Decimal
    per_household_cap: Decimal
    household_types: frozenset[str]
    scopes: frozenset[str]
    valid_from: date
    valid_to: date


@dataclass(frozen=True, slots=True)
class RuleSpec:
    rule_id: str
    version: int
    source_order: tuple[str, ...]
    source_caps: Mapping[str, Decimal]
    household_residual: bool


@dataclass(frozen=True, slots=True)
class LineSpec:
    seq: int
    source: str
    batch_id: str | None
    amount: Decimal
    bindings: tuple[str, ...]
    explanation: str


BINDING_LABELS = {
    "source_cap": "触及来源占比上限",
    "household_cap": "触及每户封顶",
    "batch_available": "触及批次可用余额",
    "remaining_need": "按剩余造价需求分摊",
    "residual": "政府资金覆盖不足，余额由家庭自筹承担",
}

# 多个上限同时命中时按此顺序解释，保证结果确定。
_BINDING_PRIORITY = ("source_cap", "household_cap", "batch_available", "remaining_need")


def compute_allocation(
    *,
    estimated_cost: Decimal,
    household_type: str,
    scope: str,
    as_of: date,
    rule: RuleSpec,
    batches: Sequence[BatchSpec],
) -> list[LineSpec]:
    """按规则版本和批次快照计算分摊明细，同样输入必然得到同样结果。"""
    if estimated_cost <= ZERO:
        raise ValueError("estimated_cost 必须大于零")
    remaining = quantize_money(estimated_cost)
    lines: list[LineSpec] = []
    seq = 0
    for source in rule.source_order:
        if remaining <= ZERO:
            break
        cap_percent = rule.source_caps.get(source)
        source_room = (
            None
            if cap_percent is None
            else quantize_money(estimated_cost * cap_percent / HUNDRED)
        )
        source_used = ZERO
        eligible = sorted(
            (batch for batch in batches if batch.source == source),
            key=lambda batch: (batch.valid_to, batch.batch_id),
        )
        for batch in eligible:
            if remaining <= ZERO or (source_room is not None and source_used >= source_room):
                break
            if household_type not in batch.household_types or scope not in batch.scopes:
                continue
            if not batch.valid_from <= as_of <= batch.valid_to or batch.available <= ZERO:
                continue
            candidates: list[tuple[Decimal, str]] = [(remaining, "remaining_need")]
            if source_room is not None:
                candidates.append((quantize_money(source_room - source_used), "source_cap"))
            candidates.append((quantize_money(batch.per_household_cap), "household_cap"))
            candidates.append((quantize_money(batch.available), "batch_available"))
            amount = min(value for value, _ in candidates)
            if amount <= ZERO:
                continue
            bound = {name for value, name in candidates if value == amount}
            bindings = tuple(name for name in _BINDING_PRIORITY if name in bound)
            seq += 1
            lines.append(
                LineSpec(
                    seq=seq,
                    source=source,
                    batch_id=batch.batch_id,
                    amount=amount,
                    bindings=bindings,
                    explanation=_explain(batch, amount, bindings, cap_percent, household_type, scope),
                )
            )
            source_used += amount
            remaining = quantize_money(remaining - amount)
    if remaining > ZERO:
        if not rule.household_residual:
            raise InsufficientFunds("政府资金批次不足以覆盖改造造价，且规则不允许家庭自筹兜底")
        seq += 1
        lines.append(
            LineSpec(
                seq=seq,
                source="household",
                batch_id=None,
                amount=remaining,
                bindings=("residual",),
                explanation=(
                    f"家庭自筹：政府资金按规则分摊后仍缺 {decimal_text(remaining)} 元，"
                    "由家庭自筹承担，不占用任何批次额度"
                ),
            )
        )
    return lines


def _explain(
    batch: BatchSpec,
    amount: Decimal,
    bindings: Sequence[str],
    cap_percent: Decimal | None,
    household_type: str,
    scope: str,
) -> str:
    parts: list[str] = []
    for name in bindings:
        if name == "source_cap":
            parts.append(f"触及来源占比上限 {decimal_text(cap_percent)}%")
        elif name == "household_cap":
            parts.append(f"触及每户封顶 {decimal_text(quantize_money(batch.per_household_cap))} 元")
        elif name == "batch_available":
            parts.append(f"触及批次可用余额 {decimal_text(quantize_money(batch.available))} 元")
        else:
            parts.append(BINDING_LABELS[name])
    reason = "、".join(parts)
    return (
        f"{SOURCE_LABELS[batch.source]}：适用户别{HOUSEHOLD_TYPE_LABELS[household_type]}、"
        f"改造范围{SCOPE_LABELS[scope]}，分摊 {decimal_text(amount)} 元（{reason}）"
    )


def distribute_writeoff(
    lines: Sequence[tuple[int, Decimal]],
    accepted: Decimal,
) -> tuple[list[dict[str, object]], Decimal]:
    """按分摊顺序核销实际验收量，返回每行核销与释放金额及超出冻结的余额。

    lines 为 (行号, 剩余冻结金额)，核销严格按行号顺序冲减，未核销部分释放；
    验收金额超出冻结总额的部分作为家庭自行承担的余额返回，不写入任何批次。
    """
    if accepted < ZERO:
        raise ValueError("accepted 不能为负数")
    remaining = quantize_money(accepted)
    result: list[dict[str, object]] = []
    for seq, frozen in lines:
        if frozen < ZERO:
            raise ValueError("冻结金额不能为负数")
        write = min(remaining, frozen)
        remaining = quantize_money(remaining - write)
        result.append(
            {
                "seq": seq,
                "write_off": quantize_money(write),
                "release": quantize_money(frozen - write),
            }
        )
    return result, remaining
