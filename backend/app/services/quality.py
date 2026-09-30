"""版本生效台：质量阈值口径的版本管理、发布重算与检验结论统一读模型。

关键约束（对应业务口径）：
- 判定只能基于「已生效」版本产出；草稿/审批中/待发布/发布中都不允许落判定；
- 阈值冲突时以最新审批生效的版本为准（发布按版本 id 单调推进，禁止回落旧版）；
- 发布与重算在同一事务里落库，失败整体回滚；
- 发布主张（claim）单独事务先行提交，中断后从失败那一版重新取；
- 已编录/待复检岩心换版即按新口径重算并转入待复检；送样中/已归还的历史留档不动；
- quality_conclusion 是检验结论的唯一事实源，台账/送样清单/编录待办都从这里读。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import quality_engine as engine
from app.store import store

VERSION_MODULE = "quality_rule_version"
CONCLUSION_MODULE = "quality_conclusion"
SUBMISSION_MODULE = "quality_submission"
STATE_MODULE = "quality_publish_state"

DRAFT, REVIEW, WAIT_PUBLISH, PUBLISHING, EFFECTIVE, EXPIRED = (
    "草稿", "审批中", "待发布", "发布中", "已生效", "已失效",
)
# 换版重算要回溯迁移的岩心状态：已编录与待复检；送样中/已归还的历史送样不动
RECALC_STATUSES = {"已编录", "待复检"}


class PublishInterrupted(RuntimeError):
    """发布在重算后、提交前中断：版本未生效，重算已随事务回滚，可从失败版恢复。"""


class QualityService:
    # 测试缝：模拟某版本重算后崩溃。生产路径永远为 None。
    fault_version_id: int | None = None

    # ---------- 版本生命周期 ----------

    def list_versions(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(VERSION_MODULE)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return sorted(rows, key=lambda row: int(row["id"]))

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        return store.find(VERSION_MODULE, version_id)

    def current_version(self) -> dict[str, Any] | None:
        effectives = [row for row in store.rows(VERSION_MODULE) if row["status"] == engine.EFFECTIVE_STATUS]
        return max(effectives, key=lambda row: int(row["id"]), default=None)

    def create_version(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        title = str(values.get("title") or "").strip()
        if not title:
            return None, "口径版本必须填写标题"
        try:
            rules = engine.normalize_rules(list(values.get("rules") or []))
        except engine.RuleError as exc:
            return None, str(exc)
        now = self._now()
        version_id = store.next_id(VERSION_MODULE)
        version_no = str(values.get("version_no") or "").strip() or f"V{now[:10]}-{version_id:03d}"
        if any(row["version_no"] == version_no for row in store.rows(VERSION_MODULE)):
            return None, f"版本号「{version_no}」已存在"
        version = {
            "id": version_id,
            "version_no": version_no,
            "title": title,
            "status": DRAFT,
            "rules": rules,
            "approved_at": None,
            "effective_at": None,
            "remark": str(values.get("remark") or "").strip(),
        }
        store.rows(VERSION_MODULE).append(version)
        return version, None

    def submit_for_approval(self, version_id: int) -> tuple[dict[str, Any] | None, str | None]:
        version = store.find(VERSION_MODULE, version_id)
        if version is None:
            return None, f"口径版本 {version_id} 不存在"
        if version["status"] != DRAFT:
            return None, f"只有草稿版本能提交审批，当前为{version['status']}"
        version["status"] = REVIEW
        return version, None

    def approve(self, version_id: int) -> tuple[dict[str, Any] | None, str | None]:
        """审批通过进入待发布；真正生效发生在发布动作里（审批 ≠ 生效）。"""
        version = store.find(VERSION_MODULE, version_id)
        if version is None:
            return None, f"口径版本 {version_id} 不存在"
        if version["status"] != REVIEW:
            return None, f"只有审批中的版本能通过审批，当前为{version['status']}"
        version["status"] = WAIT_PUBLISH
        version["approved_at"] = self._now()
        return version, None

    def reject(self, version_id: int) -> tuple[dict[str, Any] | None, str | None]:
        version = store.find(VERSION_MODULE, version_id)
        if version is None:
            return None, f"口径版本 {version_id} 不存在"
        if version["status"] != REVIEW:
            return None, f"只有审批中的版本能驳回，当前为{version['status']}"
        version["status"] = DRAFT
        version["approved_at"] = None
        return version, None

    # ---------- 发布与重算（两阶段事务） ----------

    def checkpoint(self) -> dict[str, Any]:
        return dict(self._cursor())

    def publish_pending(self) -> dict[str, Any]:
        """发布所有待发布版本；若游标指向中断版本，从那一版重新取。

        每个版本两步：
        1. claim（独立事务）：抢占版本（待发布/发布中→发布中），记下游标；
        2. activate（独立事务）：旧版失效 + 新版生效 + 已编录岩心重算转待复检，
           任一步失败整体回滚，游标改记失败版，等待从该版恢复。
        """
        cursor = self._cursor()
        start_id = cursor.get("failed_version_id") or cursor.get("pending_version_id")
        resumed = start_id is not None
        if start_id is None:
            waiting = sorted(
                (row for row in store.rows(VERSION_MODULE) if row["status"] == WAIT_PUBLISH),
                key=lambda row: int(row["id"]),
            )
            if not waiting:
                return {"ok": True, "message": "没有待发布的口径版本", "published": [], "resumed_from": None}
            start_id = int(waiting[0]["id"])

        published: list[dict[str, Any]] = []
        recalculated: dict[str, list[int]] = {}
        current_id = int(start_id)
        while True:
            version = store.find(VERSION_MODULE, current_id)
            if version is None:
                raise PublishInterrupted(f"发布游标指向的版本 {current_id} 不存在，请核对版本")
            if version["status"] not in {WAIT_PUBLISH, PUBLISHING}:
                # 恢复时若发现游标落后于已生效版本，直接报错而不是悄悄重发旧版
                if int(version["id"]) <= int(cursor["last_effective_version_id"] or 0):
                    raise PublishInterrupted(
                        f"版本 {version['version_no']} 早于或等于当前生效版本，禁止回落旧阈值"
                    )
                raise PublishInterrupted(
                    f"版本「{version['version_no']}」状态为{version['status']}，无法发布"
                )

            self._claim(version, cursor)
            try:
                migrated = self._activate_and_recalc(version, cursor)
            except BaseException as exc:
                self._mark_failed(version, cursor, exc)
                raise PublishInterrupted(
                    f"版本「{version['version_no']}」发布中断：生效与重算已整体回滚，"
                    f"请从该版本重新发布。原因：{exc}"
                ) from exc
            recalculated[version["version_no"]] = migrated
            published.append({"version_id": version["id"], "version_no": version["version_no"], "recalculated": migrated})
            # 该版本已重新发布成功，清掉一次性的中断注入，避免恢复后再次自伤
            self.fault_version_id = None

            nxt = min(
                (row for row in store.rows(VERSION_MODULE) if row["status"] == WAIT_PUBLISH),
                key=lambda row: int(row["id"]),
                default=None,
            )
            if nxt is None:
                break
            current_id = int(nxt["id"])

        return {
            "ok": True,
            "resumed_from": start_id if resumed else None,
            "published": published,
            "recalculated": recalculated,
            "message": self._publish_message(published, resumed, start_id),
        }

    def _claim(self, version: dict[str, Any], cursor: dict[str, Any]) -> None:
        """阶段一：抢占发布权。单独事务提交，保证中断后恢复点不丢。"""
        with store.transaction():
            last_eff = int(cursor["last_effective_version_id"] or 0)
            if int(version["id"]) <= last_eff:
                raise PublishInterrupted("只能向更新版本发布，禁止回落到旧阈值")
            version["status"] = PUBLISHING
            cursor["pending_version_id"] = version["id"]
            cursor["failed_version_id"] = None
            cursor["status"] = PUBLISHING
            cursor["updated_at"] = self._now()
            cursor["detail"] = f"版本「{version['version_no']}」已进入发布，等待生效与回溯重算"

    def _activate_and_recalc(self, version: dict[str, Any], cursor: dict[str, Any]) -> list[int]:
        """阶段二：生效 + 重算，必须同一事务同生共死。"""
        with store.transaction():
            now = self._now()
            for old in store.rows(VERSION_MODULE):
                if old["status"] == engine.EFFECTIVE_STATUS:
                    old["status"] = EXPIRED
            version["status"] = engine.EFFECTIVE_STATUS
            version["effective_at"] = now

            migrated: list[int] = []
            for core in store.rows("core"):
                if core.get("status") not in RECALC_STATUSES:
                    continue
                self.append_conclusion(core, version, basis="换版重算", now=now)
                core["status"] = "待复检"
                core["pending"] = True
                migrated.append(int(core["id"]))

            cursor["last_effective_version_id"] = version["id"]
            cursor["pending_version_id"] = None
            cursor["failed_version_id"] = None
            cursor["status"] = "已完成"
            cursor["updated_at"] = now
            cursor["detail"] = (
                f"版本「{version['version_no']}」已审批生效，按新口径回溯重算 {len(migrated)} 条岩心"
            )

            # 测试缝：重算完成、事务提交前注入中断，验证整体回滚与断点续发
            if self.fault_version_id == int(version["id"]):
                raise RuntimeError("注入故障：重算完成后发布中断")
            return migrated

    def _mark_failed(self, version: dict[str, Any], cursor: dict[str, Any], exc: BaseException) -> None:
        """失败游标独立事务落库：这一步本身不能回滚，否则就没有恢复点。"""
        with store.transaction():
            cursor["status"] = "发布失败"
            cursor["failed_version_id"] = version["id"]
            cursor["updated_at"] = self._now()
            cursor["detail"] = f"版本「{version['version_no']}」发布失败：{exc}；将从该版本重新发布"

    def _publish_message(self, published: list[dict[str, Any]], resumed: bool, start_id: int | None) -> str:
        if not published:
            return "没有版本被发布"
        head = f"已从失败版本 {start_id} 断点续发，" if resumed else ""
        parts = [f"{item['version_no']}（重算 {len(item['recalculated'])} 条）" for item in published]
        return f"{head}{'、'.join(parts)} 已依次生效"

    # ---------- 判定与结论 ----------

    def append_conclusion(
        self,
        core: dict[str, Any],
        version: dict[str, Any],
        *,
        basis: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """按指定版本对岩心判定并追加结论。版本必须已生效，否则拒绝落库。"""
        verdict = engine.evaluate(
            version,
            borehole=str(core.get("所属钻孔") or ""),
            depth_from=core.get("取样深度起"),
            recovery=core.get("采取率"),
        )
        if verdict["result"] == "无适用规则":
            raise engine.RuleError(
                f"生效口径「{version['version_no']}」未覆盖岩心 {core.get('岩心编号')} "
                f"的钻孔/深度区间，无法保存判定"
            )
        now = now or self._now()
        conclusions = store.rows(CONCLUSION_MODULE)
        for old in conclusions:
            if old["core_id"] == core["id"] and old.get("current"):
                old["current"] = False
                old["superseded_at"] = now
        conclusion = {
            "id": store.next_id(CONCLUSION_MODULE),
            "core_id": int(core["id"]),
            "result": verdict["result"],
            "recovery": verdict["recovery"],
            "matched_rule": verdict["matched_rule"],
            "version_id": verdict["version_id"],
            "version_no": verdict["version_no"],
            "basis": basis,
            "current": True,
            "superseded_at": None,
            "created_at": now,
        }
        conclusions.append(conclusion)
        core["abnormal"] = verdict["result"] == "不合格"
        return conclusion

    def current_conclusion(self, core_id: int) -> dict[str, Any] | None:
        rows = [row for row in store.rows(CONCLUSION_MODULE) if row["core_id"] == core_id]
        current = [row for row in rows if row.get("current")]
        if current:
            return max(current, key=lambda row: int(row["id"]))
        return max(rows, key=lambda row: int(row["id"]), default=None)

    def conclusion_history(self, core_id: int) -> list[dict[str, Any]]:
        return sorted(
            (row for row in store.rows(CONCLUSION_MODULE) if row["core_id"] == core_id),
            key=lambda row: int(row["id"]),
        )

    def freeze_submission(self, core: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        """送样：把当前结论连同命中阈值、口径版本一起冻结留档。

        历史送样结论按送样当时的口径留档；这张表一旦写入，后续换版不再改动。
        """
        conclusion = self.current_conclusion(int(core["id"]))
        if conclusion is None:
            raise engine.RuleError("该岩心尚未按生效口径形成检验结论，不能送样")
        if store.find(VERSION_MODULE, int(conclusion["version_id"])) is None:
            raise engine.RuleError("结论对应的口径版本已不存在，拒绝送样留档")
        now = now or self._now()
        sub_id = store.next_id(SUBMISSION_MODULE)
        submission = {
            "id": sub_id,
            "submission_no": f"SUB-{now[:10].replace('-', '')}-{sub_id:04d}",
            "core_id": int(core["id"]),
            "frozen_conclusion_id": int(conclusion["id"]),
            "frozen_result": conclusion["result"],
            "frozen_recovery": conclusion["recovery"],
            "frozen_version_id": conclusion["version_id"],
            "frozen_version_no": conclusion["version_no"],
            "frozen_rule": dict(conclusion["matched_rule"]),
            "submitted_at": now,
            "status": "送样中",
        }
        store.rows(SUBMISSION_MODULE).append(submission)
        return submission

    # ---------- 三个统一读视图：台账 / 送样清单 / 编录待办 ----------

    def ledger(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """岩心台账：岩心字段 + 当前检验结论。结论取自统一结论表，不在岩心表里另存。"""
        rows = store.rows("core")
        items = [self._with_conclusion(row) for row in rows]
        return self._filter_page(items, keyword=keyword, status=status, page=page, size=size,
                                 key="岩心编号")

    def submissions(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """送样清单：读送样当时冻结的结论记录（按 frozen_conclusion_id 指向同一张结论表）。"""
        core_index = {int(row["id"]): row for row in store.rows("core")}
        items = []
        for sub in store.rows(SUBMISSION_MODULE):
            core = core_index.get(int(sub["core_id"]), {})
            items.append({
                **dict(sub),
                "岩心编号": core.get("岩心编号"),
                "所属钻孔": core.get("所属钻孔"),
                "取样深度起": core.get("取样深度起"),
                "检验结论": sub["frozen_result"],
                "口径版本": sub["frozen_version_no"],
                "采取率": sub["frozen_recovery"],
                "conclusion_id": sub["frozen_conclusion_id"],
                "留档说明": "按送样当时口径留档",
            })
        total_all = items
        if keyword:
            total_all = [row for row in total_all if keyword in str(row.get("submission_no", ""))
                         or keyword in str(row.get("岩心编号", ""))]
        if status:
            total_all = [row for row in total_all if row.get("status") == status]
        total = len(total_all)
        start = max(page - 1, 0) * size
        return total_all[start:start + size], total
    def review_todos(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """编录待办：换版重算后转入待复检的岩心，携带重算所依据的新口径结论。"""
        items = [
            self._with_conclusion(row)
            for row in store.rows("core")
            if row.get("status") == "待复检"
        ]
        return self._filter_page(items, keyword=keyword, status=None, page=page, size=size,
                                 key="岩心编号")
    def _with_conclusion(self, core: dict[str, Any]) -> dict[str, Any]:
        conclusion = self.current_conclusion(int(core["id"]))
        row = dict(core)
        if conclusion is not None:
            row.update({
                "检验结论": conclusion["result"],
                "口径版本": conclusion["version_no"],
                "命中阈值": (conclusion["matched_rule"] or {}).get("min_recovery"),
                "结论依据": conclusion["basis"],
                "结论时间": conclusion["created_at"],
                "conclusion_id": int(conclusion["id"]),
            })
        else:
            row.update({
                "检验结论": None, "口径版本": None, "命中阈值": None,
                "结论依据": None, "结论时间": None, "conclusion_id": None,
            })
        return row

    def _filter_page(
        self,
        items: list[dict[str, Any]],
        *,
        keyword: str | None,
        status: str | None,
        page: int,
        size: int,
        key: str,
    ) -> tuple[list[dict[str, Any]], int]:
        if keyword:
            items = [row for row in items if keyword in str(row.get(key, ""))]
        if status:
            items = [row for row in items if row.get("status") == status]
        total = len(items)
        start = max(page - 1, 0) * size
        return items[start:start + size], total

    # ---------- 内部 ----------

    def _cursor(self) -> dict[str, Any]:
        rows = store.rows(STATE_MODULE)
        if not rows:
            cursor = {
                "id": 1,
                "scope": "global",
                "last_effective_version_id": None,
                "pending_version_id": None,
                "status": "空闲",
                "failed_version_id": None,
                "updated_at": self._now(),
                "detail": "尚未发布过口径版本",
            }
            rows.append(cursor)
        return rows[0]

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")


quality_service = QualityService()
