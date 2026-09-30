"""岩心管理业务规则：状态流转、字段校验、质量阈值判定与送样留档都收在这里。

状态序列：待编录 →（地质编录）→ 待复检 →（复检确认）→ 已编录 →（送样分析）→ 送样中 →（归还原箱）→ 已归还

口径要点：
- 地质编录那一刻按"当前最新生效版本"判定，判定写不进（没有生效规则可套）就不允许编录；
- 版本换版重算由版本生效台负责，会把已编录/待复检岩心统一打回"待复检"；
- 送样分析时把当时判定冻结成送样留档，并在同一事务里同步样品登记（送样清单）；
  之后再换版不会回溯送样中/已归还的岩心，台账读到的历史结论就是送样当时口径。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import quality_engine as engine
from app.store import store

MODULE = "core"
SAMPLE_MODULE = "sample_registry"
REQUIRED_FIELDS = ["岩心编号", "所属钻孔", "取样深度起"]
STATUS_ORDER = ["待编录", "待复检", "已编录", "送样中", "已归还"]
# 动作 -> (目标状态, 允许来源状态)
ACTION_RULES: dict[str, tuple[str, set[str]]] = {
    "地质编录": ("待复检", {"待编录"}),
    "复检确认": ("已编录", {"待复检"}),
    "送样分析": ("送样中", {"已编录"}),
    "归还原箱": ("已归还", {"送样中"}),
}
NEGATIVE_ACTIONS: set[str] = set()

# 判定结果在台账与送样清单间共用的字段（单一结果，多处读同一份）
VERDICT_FIELDS = ["检验结论", "判定版本号", "判定规则", "判定阈值", "判定时间"]


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class CoreService:
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
            rows = [row for row in rows if keyword in str(row.get("岩心编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def review_todos(self) -> list[dict[str, Any]]:
        """编录待办：口径调整后等待复检的岩心队列，读的就是台账行本身（同一份结果）。"""
        return [
            row for row in sorted(store.rows(MODULE), key=lambda r: int(r.get("id", 0)))
            if row.get("status") == "待复检"
        ]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        if any(row.get("岩心编号") == str(values.get("岩心编号")).strip() for row in rows):
            return None, ["岩心编号"]  # 调用方按"重复"语义解释
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        def _depth(field: str) -> Any:
            parsed = engine.to_float(values.get(field))
            return parsed if parsed is not None else values.get(field)

        entry.update({
            "岩心编号": str(values.get("岩心编号")).strip(),
            "所属钻孔": str(values.get("所属钻孔") or "").strip(),
            "取样深度起": _depth("取样深度起"),
            "取样深度止": _depth("取样深度止"),
            "岩性描述": str(values.get("岩性描述") or "").strip(),
            "采取率": engine.to_float(values.get("采取率")),
            "存放位置": str(values.get("存放位置") or "").strip(),
            "样本状态": "箱内待编录",
        })
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"岩心样本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于岩心管理可执行范围"
        target, allowed = ACTION_RULES[action]
        current = str(entry.get("status") or "")
        if current not in allowed:
            return None, f"岩心 {entry.get('岩心编号')} 当前为「{current}」，不能执行{action}"

        if action == "地质编录":
            return self._catalog(entry)
        if action == "复检确认":
            return self._confirm_review(entry)
        if action == "送样分析":
            return self._send_sample(entry)
        # 归还原箱
        with store.transaction():
            entry["status"] = target
            entry["pending"] = False
            entry["abnormal"] = bool(entry.get("送样冻结结论") == engine.UNQUALIFIED)
            entry["样本状态"] = "已归还入库"
        return entry, f"岩心样本已{action}"

    # ---------- 动作实现 ----------
    def _catalog(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """地质编录：按当前最新生效口径出判定；口径缺失时整笔失败，不保存无口径判定。"""
        try:
            with store.transaction():
                verdict = engine.evaluate(entry, judged_at=_now())
                entry.update(verdict)
                entry["status"] = "待复检"
                entry["pending"] = True
                entry["abnormal"] = verdict["检验结论"] == engine.UNQUALIFIED
                entry["样本状态"] = "编录完成待复检"
        except ValueError as exc:
            return None, str(exc)
        return entry, f"岩心 {entry.get('岩心编号')} 已按版本 {entry.get('判定版本号')} 编录，结论：{entry.get('检验结论')}，待复检"

    def _confirm_review(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """复检确认：沿用台账上这一版判定，不重新取阈值（取没取新口径由版本发布重算负责）。"""
        if not entry.get("判定版本号"):
            return None, f"岩心 {entry.get('岩心编号')} 缺少质量判定，不能复检确认"
        with store.transaction():
            entry["status"] = "已编录"
            entry["pending"] = False
            entry["abnormal"] = entry.get("检验结论") == engine.UNQUALIFIED
            entry["样本状态"] = "箱内完好"
        return entry, f"岩心 {entry.get('岩心编号')} 复检确认（{entry.get('判定版本号')}：{entry.get('检验结论')}）"

    def _send_sample(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """送样分析：冻结送样当时口径，并在同一事务同步送样清单。

        历史送样结论按送样当时的口径留档：这里冻结的版本号/阈值/结论之后不会再被换版改动。
        """
        if not entry.get("判定版本号"):
            return None, f"岩心 {entry.get('岩心编号')} 缺少质量判定，不能送样"
        now = _now()
        with store.transaction():
            verdict = {field: entry.get(field) for field in VERDICT_FIELDS}
            entry["status"] = "送样中"
            entry["pending"] = False
            entry["abnormal"] = verdict["检验结论"] == engine.UNQUALIFIED
            entry["样本状态"] = "已封箱送样"
            entry["送样时间"] = now
            entry.update({
                "送样编号": entry.get("送样编号") or self._alloc_sample_code(),
                "送样冻结结论": verdict["检验结论"],
                "送样冻结版本号": verdict["判定版本号"],
                "送样冻结规则": verdict["判定规则"],
                "送样冻结阈值": verdict["判定阈值"],
            })
            self._sync_sample_registry(entry, verdict, now)
        return entry, (
            f"岩心 {entry.get('岩心编号')} 已送样（{entry.get('送样编号')}），"
            f"结论按版本 {entry.get('送样冻结版本号')} 冻结留档：{entry.get('送样冻结结论')}"
        )

    def _sync_sample_registry(self, core: dict[str, Any], verdict: dict[str, Any], now: str) -> None:
        """送样清单（样品登记）与岩心台账写同一份判定：同一事务内 upsert 关联行。"""
        rows = store.rows(SAMPLE_MODULE)
        sample_code = core.get("送样编号")
        linked = next((row for row in rows if row.get("岩心编号") == core.get("岩心编号")), None)
        payload = {
            "岩心编号": core.get("岩心编号"),
            "采样位置": f"{core.get('所属钻孔')} {core.get('取样深度起')}~{core.get('取样深度止')}m",
            **verdict,
        }
        if linked is None:
            linked = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "送检编号": sample_code,
                "样品名称": core.get("岩心编号"),
                "检测项目": "岩心采取率与岩矿鉴定",
                "送检单位": "中心实验室",
                "收样日期": now[:10],
                "送检状态": "检测中",
                "status": "检测中",
                "pending": True,
                "abnormal": verdict["检验结论"] == engine.UNQUALIFIED,
            }
            linked.update(payload)
            rows.append(linked)
        else:
            # 已有的送样行：权威判定同步到清单列，但送样冻结留档字段以送样当时为准、不覆盖
            linked.update(payload)
            linked["送检编号"] = linked.get("送检编号") or sample_code
            linked["abnormal"] = verdict["检验结论"] == engine.UNQUALIFIED

    def _alloc_sample_code(self) -> str:
        rows = store.rows(SAMPLE_MODULE)
        number = max(
            (int(str(row.get("送检编号", "SAMP-0000")).split("-")[-1]) for row in rows if str(row.get("送检编号", "")).startswith("SAMP-")),
            default=0,
        ) + 1
        return f"SAMP-{number:04d}"
