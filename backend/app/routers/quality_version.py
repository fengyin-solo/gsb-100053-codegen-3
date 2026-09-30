"""质量阈值"版本生效台"接口：阈值版本的建版、改规则、审批与发布重算。

口径：
- 岩心按所属钻孔与取样深度区间套用规则；阈值冲突以最新审批生效版本为准；
- 发布与重算同一事务落库，失败一起回滚；中断后续发从失败那一版重新取，不回落旧阈值；
- 未生效版本的判定不允许保存（编录/重算只读"已生效"版本，规则在服务层强校验）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.quality_version import QualityVersionService
from app.store import store

router = APIRouter(prefix="/api/quality/versions", tags=["版本生效台"])

service = QualityVersionService()


class VersionPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    rules: list[dict[str, Any]] | None = None
    remark: str | None = None


@router.get("/checkpoint")
def get_checkpoint() -> dict[str, Any]:
    """读取发布中断检查点：没有中断时 resumed 为 false。"""
    checkpoint = service.checkpoint()
    return {"interrupted": bool(checkpoint), "checkpoint": checkpoint}


@router.get("")
def list_versions() -> dict[str, Any]:
    """版本生效台：版本清单（含每版规则）与当前发布检查点。"""
    return {
        "items": service.list_versions(),
        "total": len(store.rows("quality_versions")),
        "checkpoint": service.checkpoint(),
    }


@router.post("")
def create_version(payload: VersionPayload) -> ActionResult:
    """建阈值版本草稿并带上规则；规则非法时整笔拒绝，不保存半成品版本。"""
    entry, error = service.create_version(payload.values, payload.rules)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=f"版本 {entry['版本号']} 已建为草稿，可继续编辑或提交审批", entry=entry)


@router.put("/{code}")
def update_version(code: str, payload: VersionPayload) -> ActionResult:
    """改未生效版本的说明与规则；已生效版本不允许改，要调口径请发新版。"""
    entry, error = service.update_version(code, payload.values, payload.rules)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=f"版本 {code} 已更新", entry=entry)


@router.post("/{code}/submit")
def submit_approval(code: str) -> ActionResult:
    """提交审批：草稿/驳回后的版本进入待审批。"""
    entry, error = service.submit_approval(code)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=f"版本 {code} 已提交审批", entry=entry)


@router.post("/{code}/approve")
def approve(code: str) -> ActionResult:
    """审批通过：标记已审批，但在发布前不产生任何判定效力。"""
    entry, error = service.approve(code)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=error or f"版本 {code} 已审批，等待发布生效", entry=entry)


@router.post("/{code}/reject")
def reject(code: str, payload: VersionPayload) -> ActionResult:
    """驳回：版本退回可编辑状态，可改规则后重新提交。"""
    reason = str(payload.values.get("reason") or payload.remark or "")
    entry, error = service.reject(code, reason)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=f"版本 {code} 已驳回", entry=entry)


@router.post("/publish")
def publish(payload: VersionPayload | None = None) -> ActionResult:
    """发布已审批版本并同事务重算早期岩心。

    存在中断检查点时，本次调用即"续发"：从失败那一版重新取，已回滚的版本会整版重来，
    不会跳过失败版本，也不会回落到旧阈值。``fail_after`` 仅用于发布中断演练。
    """
    fail_after = None
    if payload is not None:
        fail_after_raw = payload.values.get("fail_after")
        try:
            fail_after = int(fail_after_raw) if fail_after_raw is not None else None
        except (TypeError, ValueError):
            fail_after = None
    detail, message = service.publish(fail_after=fail_after)
    if not detail:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=detail)
