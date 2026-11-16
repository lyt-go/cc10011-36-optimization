"""维度映射业务规则：状态流转、字段校验、规则版本与映射解析失效链。

规则状态推进到“已生效/已废弃”、以及调整映射优先级时，会通知 MappingEngine：
- 规则自身的“规则版本”+1，引擎全局“映射版本”+1；
- 仅定向失效包含该原始维度名的下游下钻路径与分层聚合结果，不再清空全部缓存；
- 草稿/评审中的改动不影响下游，不触发失效。
"""
from __future__ import annotations

from typing import Any

from app.services.mapping_engine import mapping_engine
from app.store import store

MODULE = "dimensionmap"
RAW_FIELD_MODULE = "dimensionfield"
REQUIRED_FIELDS = ["规则编号", "原始维度名", "标准维度名"]
# 登记时允许携带的规则明细字段
OPTIONAL_FIELDS = ["映射类型", "值转换逻辑", "默认取值", "映射优先级", "最近更新时间"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "废弃规则": "已废弃"}
# 只有这些动作会改变下游可读口径，需要推进版本并定向失效
VERSION_BUMP_ACTIONS = {"生效规则", "废弃规则"}
NEGATIVE_ACTIONS = []


class DimensionmapService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry.setdefault("映射类型", "直映射")
        entry.setdefault("映射优先级", "100")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 规则版本：新登记规则从 v1 开始，草稿态改动不推进版本
        entry["规则版本"] = 1
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档", None
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于维度映射可执行范围", None
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里", None
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS

        event = None
        if action in VERSION_BUMP_ACTIONS:
            # 规则口径变化：推进规则版本与映射版本，并按原始维度名定向失效下游缓存
            event = mapping_engine.bump_on_rule_change(entry, change_type=action)
        return entry, f"映射规则已{action}", event

    def adjust_priority(
        self,
        entry_id: int,
        priority: Any,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """调整映射优先级。

        优先级只对已生效规则改变命中顺序，因此已生效规则调优先级要推进版本、
        定向失效该维度的下游结果；草稿/评审中规则只改字段，不影响下游。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档", None
        text = str(priority or "").strip()
        if not text.lstrip("-").isdigit():
            return None, "映射优先级必须是整数", None
        entry["映射优先级"] = text
        if entry.get("status") == "已生效":
            event = mapping_engine.bump_on_rule_change(entry, change_type="调整映射优先级")
            return entry, f"映射优先级已调整为 {text}，相关维度下游缓存已定向失效", event
        return entry, f"映射优先级已调整为 {text}（规则未生效，暂不影响下游）", None

    def update_rule_logic(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """更新规则口径字段（值转换逻辑/默认取值/标准维度名/映射类型）。

        已生效规则改口径会改变下游映射结果，推进规则版本与映射版本并定向失效；
        草稿/评审中规则只改字段，不触碰下游缓存。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"映射规则 {entry_id} 不存在或已归档", None
        editable = ["值转换逻辑", "默认取值", "标准维度名", "映射类型"]
        changed = [field for field in editable if field in values and values[field] is not None]
        if not changed:
            return None, "没有可更新的规则口径字段（值转换逻辑/默认取值/标准维度名/映射类型）", None
        for field in changed:
            entry[field] = values[field]
        if entry.get("status") == "已生效":
            event = mapping_engine.bump_on_rule_change(entry, change_type="更新规则口径")
            return entry, f"规则口径已更新（{'、'.join(changed)}），相关维度下游缓存已定向失效", event
        return entry, f"规则口径已更新（{'、'.join(changed)}，规则未生效，暂不影响下游）", None

    # ------------------------------------------------------ 共享原始字段记录
    def list_raw_fields(self) -> list[dict[str, Any]]:
        """原始维度字段记录属于共享数据，维度映射职责内只允许读取，不提供写入口。"""
        return [dict(row) for row in store.rows(RAW_FIELD_MODULE)]

    def generate_dataset(self) -> dict[str, Any]:
        """生成标准维度映射数据集（幂等单例，重复提交不产生第二份）。"""
        return mapping_engine.generate_dataset()

    def current_dataset(self) -> dict[str, Any] | None:
        return mapping_engine.current_dataset()

    def chain_overview(self) -> dict[str, Any]:
        return mapping_engine.chain_overview()
