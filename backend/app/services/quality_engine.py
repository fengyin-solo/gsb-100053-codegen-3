"""质量阈值规则引擎：岩心样本按所属钻孔与取样深度区间套用规则。

规则优先级（同一份口径内）：
1. 钻孔专用规则优先于通用规则（borehole 为空表示全钻孔通用）；
2. 同为专用或同为通用时，区间更窄（depth_to - depth_from 更小）的优先；
3. 再相同则按区间起点更大的优先，保证任何一组规则都有确定性的唯一选择。

阈值冲突的跨版本优先级不在这里解决：引擎只在“某一个版本”内部选规则；
调用方负责只传入最新审批生效的版本——不允许拿未生效版本做判定。
"""
from __future__ import annotations

from typing import Any

# 判定只接受这个版本状态；草稿、审批中、待发布、发布中都不允许产出结论
EFFECTIVE_STATUS = "已生效"


class RuleError(ValueError):
    """规则本身不合法（缺字段、区间倒挂等）。"""


def to_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuleError(f"{field}必须是数值，收到：{value!r}") from exc
    if number != number:  # NaN
        raise RuleError(f"{field}不允许为 NaN")
    return number


def normalize_rule(raw: dict[str, Any]) -> dict[str, Any]:
    """把接口传入的规则规整成统一结构并做合法性校验。"""
    borehole = raw.get("borehole")
    borehole = str(borehole).strip() if borehole not in (None, "") else None
    depth_from = to_float(raw.get("depth_from"), "深度起")
    depth_to = to_float(raw.get("depth_to"), "深度止")
    min_recovery = to_float(raw.get("min_recovery"), "采取率阈值")
    if depth_from < 0 or depth_to < 0:
        raise RuleError("取样深度不能为负")
    if depth_from >= depth_to:
        raise RuleError(f"深度区间必须起小于止：{depth_from} >= {depth_to}")
    if not 0 < min_recovery <= 1:
        raise RuleError(f"采取率阈值必须在 (0, 1] 之间，收到：{min_recovery}")
    return {
        "borehole": borehole,
        "depth_from": depth_from,
        "depth_to": depth_to,
        "min_recovery": min_recovery,
    }


def normalize_rules(raw_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not raw_rules:
        raise RuleError("口径版本至少要包含一条阈值规则")
    return [normalize_rule(raw) for raw in raw_rules]


def _rule_rank(rule: dict[str, Any]) -> tuple[int, float, float]:
    # 专用规则（borehole 非空）排在通用规则之前；同档按区间窄、起点深排序
    specific = 1 if rule["borehole"] else 0
    width = rule["depth_to"] - rule["depth_from"]
    return (specific, -width, rule["depth_from"])


def match_rule(
    rules: list[dict[str, Any]],
    *,
    borehole: str,
    depth_from: float,
) -> dict[str, Any] | None:
    """按钻孔与样本取样深度起点挑规则；样本不在任何区间内则返回 None。

    判定锚点取“取样深度起”：一个样本只归属一条规则，避免跨区间时产生歧义。
    """
    candidates = [
        rule for rule in rules
        if (rule["borehole"] is None or rule["borehole"] == borehole)
        and rule["depth_from"] <= depth_from < rule["depth_to"]
    ]
    if not candidates:
        return None
    return max(candidates, key=_rule_rank)


def evaluate(
    version: dict[str, Any],
    *,
    borehole: str,
    depth_from: Any,
    recovery: Any,
) -> dict[str, Any]:
    """用指定版本对一条岩心做判定。

    只允许已生效版本进入这里；返回结论（合格/不合格/无适用规则）与命中规则快照。
    """
    if version.get("status") != EFFECTIVE_STATUS:
        raise RuleError(
            f"版本「{version.get('version_no')}」状态为{version.get('status')}，"
            "未生效版本不允许保存判定"
        )
    depth = to_float(depth_from, "取样深度起")
    rate = to_float(recovery, "采取率")
    if not 0 <= rate <= 1:
        raise RuleError(f"采取率必须在 [0, 1] 之间，收到：{rate}")
    matched = match_rule(version["rules"], borehole=borehole, depth_from=depth)
    if matched is None:
        return {
            "result": "无适用规则",
            "recovery": rate,
            "matched_rule": None,
            "version_id": version["id"],
            "version_no": version["version_no"],
        }
    result = "合格" if rate >= matched["min_recovery"] else "不合格"
    return {
        "result": result,
        "recovery": rate,
        "matched_rule": dict(matched),
        "version_id": version["id"],
        "version_no": version["version_no"],
    }
