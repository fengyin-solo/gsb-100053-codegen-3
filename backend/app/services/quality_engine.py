"""岩心质量阈值判定引擎。

"版本生效台"的口径只有这一份实现，编录判定、版本发布重算、送样冻结都从这里取结果，
保证岩心台账、送样清单、编录待办读到的是同一份判定。

规则匹配口径：
- 岩心按「所属钻孔 + 取样深度区间」套用：所属钻孔精确命中优先，``*`` 表示对所有钻孔兜底；
- 深度取岩心取样起点（``取样深度起``），必须落在规则的 ``[深度起, 深度止)`` 区间内；
- 同一口径下多条规则都能套上时，取深度起最大（最贴样本）的一条；
- 多个生效版本都能给出规则时，按"审批生效时间最新"的版本优先——阈值冲突以最新审批生效版本为准。
"""
from __future__ import annotations

from typing import Any

from app.store import store

VERSION_MODULE = "quality_versions"
RULE_MODULE = "quality_rules"

EFFECTIVE = "已生效"

# 判定结论
QUALIFIED = "合格"
UNQUALIFIED = "不合格"

# 重算范围：口径调整那一刻起，已编录岩心与待复检岩心都按新口径重算
RECALC_STATUSES = {"已编录", "待复检"}
# 已进入送样环节的岩心，历史送样结论按送样当时口径留档，不参与重算
FROZEN_STATUSES = {"送样中", "已归还"}


def to_float(value: Any) -> float | None:
    """把 '采取率'、深度、阈值一类字段解析成数字；解析不了返回 None（判定会因此失败）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def effective_versions() -> list[dict[str, Any]]:
    """已生效版本，按生效时间从旧到新（最新生效的在末尾，冲突时取它）。"""
    rows = [v for v in store.rows(VERSION_MODULE) if v.get("状态") == EFFECTIVE]
    return sorted(rows, key=lambda v: str(v.get("生效时间") or ""))


def _match_rule(rules: list[dict[str, Any]], borehole: str, depth_from: float) -> dict[str, Any] | None:
    candidates = [
        rule for rule in rules
        if rule.get("所属钻孔") in (borehole, "*")
        and (start := to_float(rule.get("深度起"))) is not None
        and (end := to_float(rule.get("深度止"))) is not None
        and start <= depth_from < end
    ]
    if not candidates:
        return None
    # 先按钻孔精确命中优先，再按深度起更大的优先
    return sorted(
        candidates,
        key=lambda r: (0 if r.get("所属钻孔") == borehole else 1, -(to_float(r.get("深度起")) or 0.0)),
    )[0]


def find_rule_for_core(
    core: dict[str, Any],
    *,
    versions: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """找岩心当前应套用的（生效版本, 规则）。

    从最新生效版本向前找：新版能覆盖就用新版，老版只在新版没有对应规则时兜底，
    且兜底必须显式给出——发布重算用 :func:`find_rule_in_version` 严格限定单版本，不允许静默回落。
    """
    borehole = str(core.get("所属钻孔") or "")
    depth_from = to_float(core.get("取样深度起"))
    if not borehole or depth_from is None:
        return None
    chain = versions if versions is not None else effective_versions()
    rules_all = store.rows(RULE_MODULE)
    for version in reversed(chain):
        code = version.get("版本号")
        rule = _match_rule([r for r in rules_all if r.get("版本号") == code], borehole, depth_from)
        if rule is not None:
            return version, rule
    return None


def find_rule_in_version(core: dict[str, Any], version: dict[str, Any]) -> dict[str, Any] | None:
    """严格只在指定版本里找规则：发布重算不允许回落到旧阈值版本。"""
    borehole = str(core.get("所属钻孔") or "")
    depth_from = to_float(core.get("取样深度起"))
    if not borehole or depth_from is None:
        return None
    rules = [r for r in store.rows(RULE_MODULE) if r.get("版本号") == version.get("版本号")]
    return _match_rule(rules, borehole, depth_from)


def describe_rule(rule: dict[str, Any]) -> str:
    threshold = to_float(rule.get("最低采取率"))
    text = "{borehole} {start:g}~{end:g}m 采取率≥{threshold:g}%".format(
        borehole="全部钻孔" if rule.get("所属钻孔") == "*" else rule.get("所属钻孔"),
        start=to_float(rule.get("深度起")) or 0.0,
        end=to_float(rule.get("深度止")) or 0.0,
        threshold=threshold if threshold is not None else 0.0,
    )
    return text


def evaluate(core: dict[str, Any], *, judged_at: str) -> dict[str, Any]:
    """按当前生效口径判定一条岩心。

    返回可直接写进岩心台账的判定字段；没有任何生效规则能覆盖时抛 ``ValueError``，
    由调用方决定如何失败——绝不在没有生效口径的情况下保存判定。
    """
    chain = effective_versions()
    matched = find_rule_for_core(core, versions=chain)
    if matched is None:
        raise ValueError(
            f"岩心 {core.get('岩心编号')} 没有可套用的生效阈值规则"
            f"（钻孔 {core.get('所属钻孔')}、深度 {core.get('取样深度起')}m）"
        )
    version, rule = matched
    threshold = to_float(rule.get("最低采取率"))
    rate = to_float(core.get("采取率"))
    if threshold is None:
        raise ValueError(f"规则 {rule.get('id')} 的最低采取率阈值不是有效数字")
    if rate is None:
        raise ValueError(f"岩心 {core.get('岩心编号')} 的采取率不是有效数字，无法判定")
    return {
        "检验结论": QUALIFIED if rate >= threshold else UNQUALIFIED,
        "判定版本号": version.get("版本号"),
        "判定规则": describe_rule(rule),
        "判定阈值": threshold,
        "判定时间": judged_at,
    }


def evaluate_with_version(
    core: dict[str, Any], *, version: dict[str, Any], judged_at: str
) -> dict[str, Any]:
    """严格按指定版本判定（发布重算用）。该版本覆盖不到直接报错，不回落旧版。"""
    rule = find_rule_in_version(core, version)
    if rule is None:
        raise ValueError(
            f"版本 {version.get('版本号')} 缺少覆盖岩心 {core.get('岩心编号')}"
            f"（钻孔 {core.get('所属钻孔')}、深度 {core.get('取样深度起')}m）的规则，发布已中止"
        )
    threshold = to_float(rule.get("最低采取率"))
    rate = to_float(core.get("采取率"))
    if threshold is None:
        raise ValueError(f"规则 {rule.get('id')} 的最低采取率阈值不是有效数字")
    if rate is None:
        raise ValueError(f"岩心 {core.get('岩心编号')} 的采取率不是有效数字，无法判定")
    return {
        "检验结论": QUALIFIED if rate >= threshold else UNQUALIFIED,
        "判定版本号": version.get("版本号"),
        "判定规则": describe_rule(rule),
        "判定阈值": threshold,
        "判定时间": judged_at,
    }
