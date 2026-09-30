"""版本生效台接口：质量阈值口径的版本编排、审批发布、断点续发与统一结论读视图。

- /api/quality/versions：口径版本草稿、送审、审批、发布
- /api/quality/publish：发布（版本生效 + 已编录岩心重算，同事务）
- /api/quality/ledger|submissions|todos：岩心台账 / 送样清单 / 编录待办，三处同源
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.quality import (
    PublishInterrupted,
    quality_service,
)

router = APIRouter(prefix="/api/quality", tags=["版本生效台"])


def _fail(message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


# ---------- 口径版本 ----------

@router.get("/versions")
def list_versions(status: str | None = Query(default=None, description="按版本状态过滤")) -> dict:
    """列出全部口径版本，可按草稿/审批中/待发布/发布中/已生效/已失效过滤。"""
    versions = quality_service.list_versions(status)
    current = quality_service.current_version()
    return {
        "items": versions,
        "total": len(versions),
        "current_version_id": current["id"] if current else None,
        "checkpoint": quality_service.checkpoint(),
    }


@router.post("/versions", response_model=ActionResult)
def create_version(payload: EntryPayload) -> ActionResult:
    """新建口径版本（草稿）；rules 为阈值规则数组。"""
    version, error = quality_service.create_version(payload.values)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=f"口径版本「{version['version_no']}」已存为草稿", entry=version)


@router.post("/versions/{version_id}/submit", response_model=ActionResult)
def submit_version(version_id: int) -> ActionResult:
    """草稿提交审批。"""
    version, error = quality_service.submit_for_approval(version_id)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=f"版本「{version['version_no']}」已提交审批", entry=version)


@router.post("/versions/{version_id}/approve", response_model=ActionResult)
def approve_version(version_id: int) -> ActionResult:
    """审批通过，进入待发布；此时尚未生效，不能拿它做判定。"""
    version, error = quality_service.approve(version_id)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=f"版本「{version['version_no']}」审批通过，等待发布", entry=version)


@router.post("/versions/{version_id}/reject", response_model=ActionResult)
def reject_version(version_id: int) -> ActionResult:
    """审批驳回，退回草稿。"""
    version, error = quality_service.reject(version_id)
    if error:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message=f"版本「{version['version_no']}」已驳回", entry=version)


# ---------- 发布与断点续发 ----------

@router.get("/publish/state")
def publish_state() -> dict:
    """读取发布游标：当前生效版本、待发布版本、上次中断版本。"""
    return quality_service.checkpoint()


@router.post("/publish", response_model=ActionResult)
def publish_pending(payload: EntryPayload | None = None) -> ActionResult:
    """发布待发布版本：版本生效与已编录岩心重算同一事务，失败整体回滚。

    发布中断后再次调用即从失败那一版重新取，不会回落到旧阈值。
    可通过 values.fault_version_id 注入一次发布中断（联调/演练用）。
    """
    if payload is not None and payload.values.get("fault_version_id") is not None:
        quality_service.fault_version_id = int(payload.values["fault_version_id"])
    try:
        result = quality_service.publish_pending()
    except PublishInterrupted as exc:
        return ActionResult(ok=False, message=str(exc), entry=quality_service.checkpoint())
    return ActionResult(ok=True, message=result["message"], entry={
        "resumed_from": result["resumed_from"],
        "published": result["published"],
        "recalculated": result["recalculated"],
        "checkpoint": quality_service.checkpoint(),
    })


# ---------- 结论历史 ----------

@router.get("/cores/{core_id}/conclusions")
def conclusion_history(core_id: int) -> dict:
    """单条岩心的结论变迁历史：换版重算会追加新结论，送样留档引用其中一条。"""
    history = quality_service.conclusion_history(core_id)
    return {"core_id": core_id, "total": len(history), "items": history}


# ---------- 统一读视图：台账 / 送样清单 / 编录待办 ----------

@router.get("/ledger", response_model=PageResult[dict])
def ledger(
    keyword: str | None = Query(default=None, description="按岩心编号检索"),
    status: str | None = Query(default=None, description="按岩心状态过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """岩心台账：每条岩心带当前检验结论与口径版本。"""
    if size > 200:
        raise _fail("每页最多 200 条，请缩小分页范围")
    items, total = quality_service.ledger(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/submissions", response_model=PageResult[dict])
def submissions(
    keyword: str | None = Query(default=None, description="按送样编号或岩心编号检索"),
    status: str | None = Query(default=None, description="送样中/已归还"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """送样清单：检验结论按送样当时口径冻结留档，不随后续换版改动。"""
    if size > 200:
        raise _fail("每页最多 200 条，请缩小分页范围")
    items, total = quality_service.submissions(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/todos", response_model=PageResult[dict])
def review_todos(
    keyword: str | None = Query(default=None, description="按岩心编号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """编录待办：口径调整后被重算转入待复检的岩心。"""
    if size > 200:
        raise _fail("每页最多 200 条，请缩小分页范围")
    items, total = quality_service.review_todos(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)
