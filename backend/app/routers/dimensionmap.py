"""维度映射接口：维护映射规则，并暴露映射解析缓存、标准维度数据集与失效链视图。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dimensionmap import DimensionmapService
from app.services.mapping_engine import mapping_engine

router = APIRouter(prefix="/api/dimensionmap", tags=["维度映射"])

service = DimensionmapService()

LIST_FIELDS = ["规则编号", "原始维度名", "标准维度名", "映射类型", "值转换逻辑", "默认取值", "映射优先级", "规则版本", "最近更新时间"]
STATUSES = ["草稿", "规则评审中", "已生效", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤维度映射列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出维度映射清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dimensionmap", "total": total, "items": items}


@router.get("/raw-fields")
def list_raw_fields() -> dict[str, Any]:
    """共享的原始维度字段记录：维度映射职责内只读，不提供写入/删除入口。"""
    items = service.list_raw_fields()
    return {"module": "dimensionfield", "total": len(items), "items": items}


@router.post("/dataset/generate", response_model=ActionResult)
def generate_dataset() -> ActionResult:
    """按优先级转换并映射标准维度，生成标准维度映射数据集。

    幂等：同一映射版本内重复提交直接复用既有数据集，不会生成第二份。
    """
    dataset = service.generate_dataset()
    repeated = bool(dataset.get("重复提交"))
    message = (
        f"数据集已存在（映射版本 v{dataset['映射版本']}），重复提交未生成第二份"
        if repeated
        else f"标准维度映射数据集已生成，映射版本 v{dataset['映射版本']}，共 {dataset['记录数']} 条"
    )
    return ActionResult(ok=True, message=message, entry=dataset)


@router.get("/dataset/current")
def current_dataset() -> dict[str, Any]:
    """读取当前标准维度映射数据集；尚未生成时给出提示而不是报错。"""
    dataset = service.current_dataset()
    if dataset is None:
        return {"generated": False, "message": "标准维度映射数据集尚未生成", "items": []}
    return {"generated": True, **dataset}


@router.get("/chain")
def chain_overview() -> dict[str, Any]:
    """映射命中 → 缓存失效 → 聚合读取的全链路视图（含命中快照、下游缓存、失效事件）。"""
    return service.chain_overview()


@router.get("/version")
def mapping_version() -> dict[str, Any]:
    """当前生效映射版本与规则版本水位，供下游页面核对数据新鲜度。"""
    rows = service.list_entries(page=1, size=10000)[0]
    return {
        "active_mapping_version": mapping_engine.active_version,
        "rule_versions": [
            {
                "规则编号": row.get("规则编号"),
                "原始维度名": row.get("原始维度名"),
                "规则版本": row.get("规则版本"),
                "status": row.get("status"),
            }
            for row in rows
        ],
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条映射规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"映射规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条映射规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="映射规则已登记（草稿态，规则版本 v1）", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条映射规则执行提交规则评审、生效规则、废弃规则。

    生效/废弃会推进规则版本与映射版本，并定向失效该维度的下钻路径与分层聚合结果。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message, event = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    if event is not None:
        entry = {**entry, "_invalidation_event": event}
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/priority", response_model=ActionResult)
def adjust_priority(entry_id: int, payload: EntryPayload) -> ActionResult:
    """调整映射优先级；已生效规则会触发版本推进与该维度的定向失效。"""
    priority = payload.values.get("映射优先级")
    entry, message, event = service.adjust_priority(entry_id, priority)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={**entry, "_invalidation_event": event} if event else entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_rule(entry_id: int, payload: EntryPayload) -> ActionResult:
    """更新规则口径（值转换逻辑/默认取值/标准维度名/映射类型）。

    已生效规则更新会推进版本并定向失效该维度的下钻与聚合结果。
    """
    entry, message, event = service.update_rule_logic(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={**entry, "_invalidation_event": event} if event else entry)
