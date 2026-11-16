"""聚合配置业务规则：规则流转 + 跨模块消费标准维度映射的分层聚合缓存。

与多维下钻同为映射结果消费方：聚合结果随映射缓存联动失效，
但聚合按“分层粒度”缓存（同一聚合规则下每个目标粒度一条），
规则变更只失效包含该原始维度的分层结果，其它粒度继续命中。

单一版本口径：聚合读取只接受当前生效映射版本的结果，版本切换后旧结果整体重算，
不会把不同映射版本的分层结果拼在同一份聚合里。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.mapping_cache import VersionedResultCache, get_or_compute
from app.services.mapping_engine import mapping_engine
from app.store import store

MODULE = "aggregate"
CONSUMER_NAME = "分层聚合"
REQUIRED_FIELDS = ["规则编号", "源数据粒度", "目标粒度"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []

aggregate_cache = VersionedResultCache(CONSUMER_NAME)
mapping_engine.register_consumer(CONSUMER_NAME, aggregate_cache)


class AggregateService:
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
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚合规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于聚合配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"聚合规则已{action}"

    # ---------------------------------------------------------- 分层聚合读取
    def _split_grains(self, target_grain: str) -> list[str]:
        """目标粒度字段先按逗号拆成多个粒度（斜杠是粒度内的维度分隔，不在此拆）。"""
        return [
            part.strip()
            for part in re.split(r"[,，、]", str(target_grain or ""))
            if part.strip()
        ]

    def _grain_dimensions(self, grain: str) -> list[str]:
        """单个粒度按斜杠拆成若干原始维度。"""
        return [
            part.strip()
            for part in str(grain or "").split("/")
            if part.strip()
        ]

    def layered_aggregate(self, entry_id: int) -> tuple[dict[str, Any] | None, str | None]:
        """读取聚合规则在各目标粒度下的分层聚合结果。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"聚合规则 {entry_id} 不存在或已归档"

        grains = self._split_grains(str(entry.get("目标粒度") or ""))
        version = mapping_engine.active_version
        layers: list[dict[str, Any]] = []
        hit_flags: list[bool] = []
        stale_flags: list[bool] = []
        conflict_collector: list[str] = []

        for index, grain in enumerate(grains, start=1):
            dimensions = self._grain_dimensions(grain)
            cache_key = f"aggregate:{entry_id}:grain:{index}"

            def _compute(grain_value: str = grain, dims: list[str] | None = None) -> dict[str, Any]:
                dims = dims or self._grain_dimensions(grain_value)
                mapped: list[dict[str, Any]] = []
                for raw_dimension in dims:
                    snapshot = mapping_engine.resolve(raw_dimension)
                    mapped.append({
                        "原始维度名": raw_dimension,
                        "标准维度名": snapshot.standard_dimension,
                        "标准值": snapshot.standard_value,
                        "命中规则编号": snapshot.rule_id or "未命中规则",
                        "规则版本": snapshot.rule_version,
                        "映射版本": snapshot.engine_version,
                        "转换状态": snapshot.convert_status,
                        "冲突规则": snapshot.conflict_rules,
                    })
                return {
                    "目标粒度": grain_value,
                    "映射版本": version,
                    "维度映射": mapped,
                }

            payload, cached, hit = get_or_compute(
                aggregate_cache, cache_key, version, set(dimensions), _compute,
            )
            hit_flags.append(hit)
            grain_conflicts = sorted({
                rule
                for mapped in payload["维度映射"]
                for rule in mapped["冲突规则"]
                if mapped["转换状态"] == "convert_error"
            })
            if grain_conflicts:
                cached = aggregate_cache.mark_stale(cache_key, grain_conflicts)
            if cached is not None:
                stale_flags.append(cached.stale)
                conflict_collector.extend(cached.conflict_rules)
            layers.append({
                **payload,
                "缓存命中": hit,
                "数据新鲜度": (
                    f"映射版本 v{cached.mapping_version}（与当前规则版本一致）"
                    if cached and not cached.stale
                    else (
                        f"映射版本 v{cached.mapping_version}（转换异常，沿用上一可用结果）"
                        if cached
                        else "未缓存"
                    )
                ),
            })

        return {
            "规则编号": entry.get("规则编号"),
            "输出数据集": entry.get("输出数据集"),
            "映射版本": version,
            "整体缓存命中": all(hit_flags) if hit_flags else False,
            "stale": any(stale_flags),
            "冲突规则": sorted(set(conflict_collector)),
            "分层聚合结果": layers,
        }, None

    def cache_snapshot(self) -> dict[str, Any]:
        return {
            "consumer": CONSUMER_NAME,
            "active_mapping_version": mapping_engine.active_version,
            "缓存明细": aggregate_cache.snapshot(),
        }
