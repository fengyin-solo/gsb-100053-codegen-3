"""岩心管理业务规则：状态流转、字段校验、质量判定与筛选口径都收在这里。

质量口径不再写死在本模块：编录判定一律走「版本生效台」当前已生效的口径版本，
未生效版本不允许保存判定；送样时由版本生效台冻结送样当时的结论与口径留档。
"""
from __future__ import annotations

from typing import Any

from app.services import quality_engine
from app.services.quality import quality_service
from app.store import store

MODULE = "core"
REQUIRED_FIELDS = ["岩心编号", "所属钻孔", "取样深度起"]
STATUS_ORDER = ["待编录", "已编录", "待复检", "送样中", "已归还"]
# 换版重算后的复检动作也在这里收口
ACTION_RULES = {"地质编录": "已编录", "送样分析": "送样中", "归还原箱": "已归还", "复检通过": "已编录"}
NEGATIVE_ACTIONS = []


class CoreService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 列表即岩心台账：检验结论统一来自版本生效台的结论表，与送样清单、编录待办同源
        return quality_service.ledger(keyword=keyword, status=status, page=page, size=size)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return quality_service._with_conclusion(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({
            "岩心编号": values.get("岩心编号"),
            "所属钻孔": values.get("所属钻孔"),
            "取样深度起": values.get("取样深度起"),
            "取样深度止": values.get("取样深度止"),
            "岩性描述": values.get("岩性描述"),
            "采取率": values.get("采取率"),
            "存放位置": values.get("存放位置"),
            "样本状态": values.get("样本状态"),
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
        target = ACTION_RULES[action]

        if action == "地质编录":
            return self._catalog(entry)
        if action == "复检通过":
            return self._pass_recheck(entry)
        if action == "送样分析":
            return self._send_sample(entry)
        # 归还原箱
        if entry.get("status") != "送样中":
            return None, "只有送样中的岩心能归还原箱"
        entry["status"] = target
        entry["pending"] = False
        for sub in store.rows("quality_submission"):
            if sub["core_id"] == entry_id and sub["status"] == "送样中":
                sub["status"] = "已归还"
        return quality_service._with_conclusion(entry), "岩心样本已归还原箱，送样留档保持送样当时口径"

    # ---------- 动作实现 ----------

    def _catalog(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if entry.get("status") not in {"待编录"}:
            return None, f"当前状态为{entry.get('status')}，不能再做地质编录"
        version = quality_service.current_version()
        if version is None:
            return None, "当前没有已生效的质量口径版本，无法按口径完成编录判定"
        try:
            with store.transaction():
                quality_service.append_conclusion(entry, version, basis="编录")
                entry["status"] = "已编录"
                entry["pending"] = False
        except quality_engine.RuleError as exc:
            return None, str(exc)
        return quality_service._with_conclusion(entry), f"已按口径版本「{version['version_no']}」编录判定"

    def _pass_recheck(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """复检通过：必须仍按当前生效口径合格才允许回到已编录；不合格继续挂待复检。"""
        if entry.get("status") != "待复检":
            return None, "只有待复检岩心能执行复检通过"
        version = quality_service.current_version()
        if version is None:
            return None, "当前没有已生效的质量口径版本，无法复检"
        try:
            verdict = quality_engine.evaluate(
                version,
                borehole=str(entry.get("所属钻孔") or ""),
                depth_from=entry.get("取样深度起"),
                recovery=entry.get("采取率"),
            )
        except quality_engine.RuleError as exc:
            return None, str(exc)
        if verdict["result"] == "无适用规则":
            return None, f"生效口径「{version['version_no']}」未覆盖该岩心的钻孔/深度区间，无法复检"
        if verdict["result"] != "合格":
            return None, (
                f"按当前生效口径「{version['version_no']}」复检仍不合格"
                f"（采取率 {verdict['recovery']} < 阈值 {verdict['matched_rule']['min_recovery']}），"
                "继续挂待复检"
            )
        with store.transaction():
            quality_service.append_conclusion(entry, version, basis="复检")
            entry["status"] = "已编录"
            entry["pending"] = False
        return quality_service._with_conclusion(entry), f"复检合格，已按「{version['version_no']}」确认结论"

    def _send_sample(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if entry.get("status") not in {"已编录", "待复检"}:
            return None, f"当前状态为{entry.get('status')}，需先完成编录/复检才能送样"
        if entry.get("status") == "待复检":
            return None, "该岩心正在待复检，复检通过前不能送样"
        try:
            with store.transaction():
                submission = quality_service.freeze_submission(entry)
                entry["status"] = "送样中"
                entry["pending"] = False
        except quality_engine.RuleError as exc:
            return None, str(exc)
        return (
            quality_service._with_conclusion(entry),
            f"已送样，结论按口径「{submission['frozen_version_no']}」冻结留档（{submission['submission_no']}）",
        )
