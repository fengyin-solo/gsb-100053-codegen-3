"""样品登记业务规则：状态流转、字段校验与筛选口径都收在这里。

送样清单里关联岩心的"检验结论/判定版本号/判定规则/判定阈值/判定时间"五列不自行保存计算结果，
每次列表/明细都回岩心台账取同一份权威判定，保证台账、送样清单、编录待办三处读到的结果一致。
只有"送样冻结*"留档列是送样当时快照，换版后也不动，用来回答"当时按哪版口径送的样"。
"""
from __future__ import annotations

from typing import Any

from app.services.core import VERDICT_FIELDS
from app.store import store

MODULE = "sample_registry"
CORE_MODULE = "core"
REQUIRED_FIELDS = ["送检编号", "样品名称", "采样位置"]
STATUS_ORDER = ["待收样", "已收样", "检测中", "已出报告"]
ACTION_RULES = {"确认收样": "已收样", "登记报告": "已出报告", "退回样品": "待收样"}
NEGATIVE_ACTIONS = []


def _project(row: dict[str, Any]) -> dict[str, Any]:
    """把岩心台账上的权威判定投影到送样清单；未关联岩心的手工送检行不受影响。"""
    item = dict(row)
    item.setdefault("岩心编号", None)
    core_code = row.get("岩心编号")
    if core_code:
        core = next((c for c in store.rows(CORE_MODULE) if c.get("岩心编号") == core_code), None)
        if core is not None:
            for field in VERDICT_FIELDS:
                item[field] = core.get(field)
            # 送样留档以送样当时冻结值为准；台账缺冻结列时用清单上已存的快照
            for field in ("送样冻结结论", "送样冻结版本号", "送样冻结规则", "送样冻结阈值", "送样时间"):
                item[field] = core.get(field) if core.get(field) is not None else row.get(field)
    return item


class SampleRegistryService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("送检编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [_project(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return _project(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"送检样品 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于样品登记可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with store.transaction():
            entry["status"] = target
            entry["送检状态"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        return _project(entry), f"送检样品已{action}"
