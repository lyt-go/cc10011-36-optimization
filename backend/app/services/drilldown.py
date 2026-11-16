"""多维下钻业务规则：路径流转 + 消费标准维度映射结果的分层下钻缓存。

职责边界：下钻模块是标准维度映射结果的消费方，只通过 MappingEngine 读取，
不直接读写 dimensionmap 规则表与共享原始字段表。

缓存口径：
- 每条下钻路径的分层结果钉住一个映射版本，结果上的“数据新鲜度”与实际规则版本一致；
- 映射规则变更时，引擎按原始维度名定向失效包含该维度的路径，未涉及维度的路径保持命中；
- 版本切换后旧版本缓存不再读取（单一映射版本口径），重新计算并回写新版本。
"""
from __future__ import annotations

from typing import Any

from app.services.mapping_cache import VersionedResultCache, get_or_compute
from app.services.mapping_engine import mapping_engine
from app.store import store

MODULE = "drilldown"
CONSUMER_NAME = "多维下钻"
REQUIRED_FIELDS = ["路径编号", "起始维度", "下钻层级序列"]
STATUS_ORDER = ["草稿", "路径评审中", "已启用", "已归档"]
ACTION_RULES = {"提交路径评审": "路径评审中", "启用路径": "已启用", "归档路径": "已归档"}
NEGATIVE_ACTIONS = []

# 下钻分层结果缓存：启动时向映射引擎登记，规则变更后由引擎定向失效
drilldown_cache = VersionedResultCache(CONSUMER_NAME)
mapping_engine.register_consumer(CONSUMER_NAME, drilldown_cache)


def _split_levels(entry: dict[str, Any]) -> list[str]:
    """下钻层级序列支持逗号/顿号/分号分隔。"""
    raw = str(entry.get("下钻层级序列") or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.replace("、", ",").replace(";", ",").split(",") if part.strip()]


class DrilldownService:
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
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
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
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维下钻可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"下钻路径已{action}"

    # ---------------------------------------------------------- 分层下钻读取
    def path_dimensions(self, entry: dict[str, Any]) -> list[str]:
        """一条路径依赖的全部原始维度：起始维度 + 各下钻层级。"""
        levels = _split_levels(entry)
        start = str(entry.get("起始维度") or "").strip()
        dimensions: list[str] = []
        for name in [start, *levels]:
            if name and name not in dimensions:
                dimensions.append(name)
        return dimensions

    def layered_result(self, entry_id: int) -> tuple[dict[str, Any] | None, str | None]:
        """读取分层下钻结果：命中缓存直接复用，未命中则经映射引擎解析后回写。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档"

        dimensions = self.path_dimensions(entry)
        version = mapping_engine.active_version
        cache_key = f"path:{entry_id}"

        def _compute() -> dict[str, Any]:
            layers: list[dict[str, Any]] = []
            for level, raw_dimension in enumerate(dimensions, start=1):
                # 结构性命中：该维度映射到哪个标准维度、命中哪条规则（与取值无关，可稳定复用）
                snapshot = mapping_engine.resolve(raw_dimension)
                # 取值样本：从共享原始字段记录解析，暴露转换异常与上一可用结果
                samples = mapping_engine.resolve_dimension_samples(raw_dimension)
                sample_errors = [s for s in samples if s.convert_status == "convert_error"]
                layers.append({
                    "层级": level,
                    "原始维度名": raw_dimension,
                    "标准维度名": snapshot.standard_dimension,
                    "标准值": snapshot.standard_value,
                    "命中规则编号": snapshot.rule_id or "未命中规则",
                    "规则版本": snapshot.rule_version,
                    "映射版本": snapshot.engine_version,
                    "转换状态": sample_errors[0].convert_status if sample_errors else snapshot.convert_status,
                    "冲突规则": sorted({
                        rule for s in samples for rule in s.conflict_rules
                    }) or snapshot.conflict_rules,
                    "上一可用标准值": sample_errors[0].fallback_value if sample_errors else None,
                    "取值样本": [s.to_dict() for s in samples],
                })
            return {
                "路径编号": entry.get("路径编号"),
                "映射版本": version,
                "层级结果": layers,
            }

        payload, cached, hit = get_or_compute(
            drilldown_cache, cache_key, version, set(dimensions), _compute,
        )
        # 转换异常：把冲突规则标记到缓存项，新鲜度提示沿用上一可用结果
        conflict_rules = sorted({
            rule
            for layer in payload["层级结果"]
            for rule in layer["冲突规则"]
            if layer["转换状态"] == "convert_error"
        })
        if conflict_rules and cached is not None:
            drilldown_cache.mark_stale(cache_key, conflict_rules)
        result = {
            **payload,
            "缓存命中": hit,
            "数据新鲜度": self._freshness_label(cached),
            "依赖原始维度": dimensions,
            "stale": cached.stale if cached else False,
            "冲突规则": cached.conflict_rules if cached else [],
        }
        return result, None

    @staticmethod
    def _freshness_label(cached: Any) -> str:
        """新鲜度必须与实际映射版本一致：展示钉住的版本号与是否沿用旧值。"""
        if cached is None:
            return "未缓存"
        if cached.stale:
            return f"映射版本 v{cached.mapping_version}（转换异常，沿用上一可用结果）"
        return f"映射版本 v{cached.mapping_version}（与当前规则版本一致）"

    def cache_snapshot(self) -> dict[str, Any]:
        return {
            "consumer": CONSUMER_NAME,
            "active_mapping_version": mapping_engine.active_version,
            "缓存明细": drilldown_cache.snapshot(),
        }
