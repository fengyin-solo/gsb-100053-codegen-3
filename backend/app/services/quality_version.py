"""质量阈值"版本生效台"：版本与规则的生命周期、审批发布、换版重算。

发布语义（对应业务口径）：
- 发布与重算在同一事务里落库：规则落"已生效"、版本状态翻转、早期岩心回溯重算任一步失败，
  整版一起回滚——不会留下半生效的版本，也不会保存任何未生效版本的判定；
- 重算严格只按正在发布的新版取规则，覆盖不到就失败，绝不回落到旧阈值；
- 已编录、待复检的早期样本要回溯迁移；送样中、已归还的历史送样结论按送样当时口径留档，不动；
- 发布中断后检查点记录失败的那一版，续发从失败版本重新取（而不是跳过它或回退到旧版）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import quality_engine as engine
from app.store import store

VERSION_MODULE = "quality_versions"
RULE_MODULE = "quality_rules"
CORE_MODULE = "core"

DRAFT = "草稿"
PENDING_APPROVAL = "待审批"
APPROVED = "已审批"
EFFECTIVE = "已生效"
REJECTED = "已驳回"
VERSION_STATES = [DRAFT, PENDING_APPROVAL, APPROVED, EFFECTIVE, REJECTED]
# 未生效的版本：这些版本的判定一律不允许保存
NOT_EFFECTIVE_STATES = {DRAFT, PENDING_APPROVAL, APPROVED, REJECTED}

CHECKPOINT_KEY = "quality_publish_checkpoint"


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


class QualityVersionService:
    # ---------- 读取 ----------
    def list_versions(self) -> list[dict[str, Any]]:
        versions = sorted(
            store.rows(VERSION_MODULE),
            key=lambda v: (str(v.get("生效时间") or ""), int(v.get("id", 0))),
        )
        rules = store.rows(RULE_MODULE)
        result: list[dict[str, Any]] = []
        for version in versions:
            code = version.get("版本号")
            version_rules = [r for r in rules if r.get("版本号") == code]
            item = dict(version)
            item["规则数"] = len(version_rules)
            item["规则"] = [dict(r) for r in sorted(version_rules, key=lambda r: int(r.get("id", 0)))]
            result.append(item)
        return result

    def get_version(self, code: str) -> dict[str, Any] | None:
        for version in store.rows(VERSION_MODULE):
            if version.get("版本号") == code:
                return version
        return None

    def checkpoint(self) -> dict[str, Any] | None:
        """上一次发布中断留下的检查点；没有中断返回 None。"""
        return store.meta(CHECKPOINT_KEY)

    def list_rules(self, version_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RULE_MODULE)
        if version_code:
            rows = [r for r in rows if r.get("版本号") == version_code]
        return [dict(r) for r in sorted(rows, key=lambda r: int(r.get("id", 0)))]

    # ---------- 版本草稿与审批 ----------
    def create_version(
        self, values: dict[str, Any], rules: list[dict[str, Any]] | None
    ) -> tuple[dict[str, Any] | None, str]:
        code = str(values.get("版本号") or "").strip()
        if not code:
            return None, "版本号必填"
        if self.get_version(code) is not None:
            return None, f"版本号 {code} 已存在，不能重复建版"
        error = self._validate_rules(rules)
        if error:
            return None, error
        now = _now()
        with store.transaction():
            version = {
                "id": _next_id(VERSION_MODULE),
                "版本号": code,
                "状态": DRAFT,
                "说明": str(values.get("说明") or "").strip(),
                "创建时间": now,
                "审批时间": None,
                "生效时间": None,
            }
            store.rows(VERSION_MODULE).append(version)
            self._replace_rules(code, rules)
        return dict(version), ""

    def update_version(
        self, code: str, values: dict[str, Any], rules: list[dict[str, Any]] | None
    ) -> tuple[dict[str, Any] | None, str]:
        version = self.get_version(code)
        if version is None:
            return None, f"版本 {code} 不存在"
        if version.get("状态") == EFFECTIVE:
            # 已生效口径是历史判定留档依据，不允许直接改；要调整口径就发新版
            return None, f"版本 {code} 已生效，阈值不能再改；请新建版本换版"
        if version.get("状态") == PENDING_APPROVAL:
            return None, f"版本 {code} 正在审批中，需先驳回或审批完成才能修改"
        error = self._validate_rules(rules)
        if error:
            return None, error
        with store.transaction():
            if values.get("说明") is not None:
                version["说明"] = str(values.get("说明") or "").strip()
            self._replace_rules(code, rules)
        return self.get_version(code), ""

    def submit_approval(self, code: str) -> tuple[dict[str, Any] | None, str]:
        version = self.get_version(code)
        if version is None:
            return None, f"版本 {code} 不存在"
        if version.get("状态") not in (DRAFT, REJECTED):
            return None, f"版本 {code} 当前为「{version.get('状态')}」，不能提交审批"
        if not [r for r in store.rows(RULE_MODULE) if r.get("版本号") == code]:
            return None, f"版本 {code} 还没有任何规则，不能提交审批"
        with store.transaction():
            version["状态"] = PENDING_APPROVAL
        return self.get_version(code), ""

    def approve(self, code: str) -> tuple[dict[str, Any] | None, str]:
        version = self.get_version(code)
        if version is None:
            return None, f"版本 {code} 不存在"
        if version.get("状态") != PENDING_APPROVAL:
            return None, f"版本 {code} 当前为「{version.get('状态')}」，只有待审批版本可以审批"
        with store.transaction():
            version["状态"] = APPROVED
            version["审批时间"] = _now()
        return self.get_version(code), ""

    def reject(self, code: str, reason: str | None = None) -> tuple[dict[str, Any] | None, str]:
        version = self.get_version(code)
        if version is None:
            return None, f"版本 {code} 不存在"
        if version.get("状态") != PENDING_APPROVAL:
            return None, f"版本 {code} 当前为「{version.get('状态')}」，不能驳回"
        with store.transaction():
            version["状态"] = REJECTED
            version["驳回原因"] = (reason or "").strip()
        return self.get_version(code), "版本已驳回，可修改后重新提交审批"

    # ---------- 发布生效与换版重算 ----------
    def publish(self, *, fail_after: int | None = None) -> tuple[dict[str, Any], str]:
        """发布所有"已审批未生效"的版本。

        每个版本与它触发的岩心重算包在同一个事务里：任一岩心按新版无规则可套或被注入失败，
        这一版（含已迁移的早期样本）整体回滚。检查点不随事务回滚，续发时从失败版本重新取。
        """
        checkpoint = store.meta(CHECKPOINT_KEY)
        resuming = bool(checkpoint)
        if resuming:
            # 上一批发布中断：必须从失败的那一版重新取，不允许跳过，也不另起一批
            queue = [code for code in checkpoint.get("queue", []) if self.get_version(code) is not None]
            # 中断之后又审批的版本排到队尾，失败版本仍然最先处理
            queued = set(queue)
            for version in sorted(store.rows(VERSION_MODULE), key=lambda v: int(v.get("id", 0))):
                if version.get("状态") == APPROVED and version.get("版本号") not in queued:
                    queue.append(version.get("版本号"))
            started_by = "续发"
        else:
            queue = [
                v.get("版本号")
                for v in sorted(store.rows(VERSION_MODULE), key=lambda v: int(v.get("id", 0)))
                if v.get("状态") == APPROVED
            ]
            if not queue:
                return {}, "没有待发布的已审批版本"
            started_by = "发布"

        published: list[str] = []
        attempts = int(checkpoint.get("attempts", 0)) if checkpoint else 0
        last_error = ""
        for code in list(queue):
            version = self.get_version(code)
            if version is None:
                last_error = f"版本 {code} 已不存在，发布中止"
                self._write_checkpoint(queue, code, last_error, attempts + 1, published)
                return {}, last_error
            if version.get("状态") == EFFECTIVE:
                # 检查点队列里已生效的版本（人工补偿等场景）直接出队
                queue.remove(code)
                continue
            try:
                migrated = self._publish_one(version, fail_after=fail_after)
            except (PublishAborted, ValueError) as exc:
                last_error = str(exc)
                # 已成功的版本出队，失败版本留在队首：续发从它重新取
                for done in published:
                    if done in queue:
                        queue.remove(done)
                self._write_checkpoint(queue, code, last_error, attempts + 1, published)
                return {}, (
                    f"{started_by}在版本 {code} 处中断，整版改动已回滚：{last_error}。"
                    f"处理好该版本规则后再次发布，将从 {code} 重新取，不会回落到旧阈值。"
                )
            published.append(code)
            # 注入的失败只用于演练一次；真实失败不会走到这里
            fail_after = None

        store.set_meta(CHECKPOINT_KEY, None)
        summary = "、".join(published) if published else ""
        return {"published": published, "migrated": migrated if published else 0}, (
            f"版本 {summary} 已发布生效，早期岩心按新口径完成回溯重算"
            if published else "没有需要发布的版本"
        )

    def _publish_one(self, version: dict[str, Any], *, fail_after: int | None = None) -> int:
        """单版本生效 + 重算，全部在同一事务；失败由调用方捕获后整版回滚。"""
        code = version.get("版本号")
        with store.transaction():
            now = _now()
            cores = sorted(
                [row for row in store.rows(CORE_MODULE) if row.get("status") in engine.RECALC_STATUSES],
                key=lambda row: int(row.get("id", 0)),
            )
            migrated: list[dict[str, Any]] = []
            for core in cores:
                verdict = engine.evaluate_with_version(core, version=version, judged_at=now)
                core.update(verdict)
                # 口径调整那一刻起，已编录岩心按新口径重算并转入待复检
                core["status"] = "待复检"
                core["pending"] = True
                core["abnormal"] = verdict["检验结论"] == engine.UNQUALIFIED
                migrated.append(core)
                if fail_after is not None and len(migrated) >= fail_after:
                    raise PublishAborted(
                        f"演练中断：版本 {code} 重算到岩心 {core.get('岩心编号')} 后中止"
                    )
            version["状态"] = EFFECTIVE
            version["生效时间"] = now
            if not version.get("审批时间"):
                version["审批时间"] = now
            return len(migrated)

    def _write_checkpoint(
        self,
        queue: list[str],
        failed_code: str,
        error: str,
        attempts: int,
        published: list[str],
    ) -> None:
        store.set_meta(CHECKPOINT_KEY, {
            "queue": queue,
            "failed_version": failed_code,
            "error": error,
            "attempts": attempts,
            "already_published": published,
            "at": _now(),
        })

    # ---------- 规则维护 ----------
    def replace_rules(self, code: str, rules: list[dict[str, Any]]) -> str:
        version = self.get_version(code)
        if version is None:
            return f"版本 {code} 不存在"
        if version.get("状态") == EFFECTIVE:
            return f"版本 {code} 已生效，规则不能再改；请新建版本换版"
        if version.get("状态") == PENDING_APPROVAL:
            return f"版本 {code} 正在审批中，需先驳回才能改规则"
        error = self._validate_rules(rules)
        if error:
            return error
        with store.transaction():
            self._replace_rules(code, rules)
        return ""

    def _replace_rules(self, code: str, rules: list[dict[str, Any]] | None) -> None:
        rows = store.rows(RULE_MODULE)
        rows[:] = [r for r in rows if r.get("版本号") != code]
        if not rules:
            return
        for raw in rules:
            borehole = str(raw.get("所属钻孔") or "").strip()
            start = engine.to_float(raw.get("深度起"))
            end = engine.to_float(raw.get("深度止"))
            threshold = engine.to_float(raw.get("最低采取率"))
            rows.append({
                "id": _next_id(RULE_MODULE),
                "版本号": code,
                "所属钻孔": borehole,
                "深度起": start,
                "深度止": end,
                "最低采取率": threshold,
            })

    def _validate_rules(self, rules: list[dict[str, Any]] | None) -> str:
        if not rules:
            return "新版本至少要有一条阈值规则"
        for index, raw in enumerate(rules, start=1):
            borehole = str(raw.get("所属钻孔") or "").strip()
            start = engine.to_float(raw.get("深度起"))
            end = engine.to_float(raw.get("深度止"))
            threshold = engine.to_float(raw.get("最低采取率"))
            if not borehole:
                return f"第 {index} 条规则缺少所属钻孔（用 * 表示全部钻孔）"
            if start is None or end is None:
                return f"第 {index} 条规则的深度区间不是有效数字"
            if not 0 <= start < end:
                return f"第 {index} 条规则深度区间非法：需要 0 ≤ 深度起 < 深度止"
            if threshold is None or not 0 <= threshold <= 100:
                return f"第 {index} 条规则的最低采取率需在 0~100 之间"
        return ""


class PublishAborted(Exception):
    """发布重算在某条岩心处中止：触发当前版本事务整体回滚。"""
