"""下游结果缓存：带映射版本号的结果缓存与按维度定向失效。

多维下钻、分层聚合都消费同一份标准维度映射结果，它们的结果缓存统一走这里：
- 每条结果都钉住一个映射版本（mapping_version），下游读取时只接受当前生效版本，
  从业务边界上保证“一个下游聚合只读取一个映射版本”，不混版本拼结果；
- 映射规则变更时按原始维度名定向失效，而不是清空全部缓存；
- 转换异常允许沿用上一可用结果，但结果上会带 stale/conflict 标记，新鲜度与实际版本一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class CachedResult:
    """单条下游结果：结果数据 + 它依赖的维度与映射版本。"""

    key: str
    payload: dict[str, Any]
    mapping_version: int
    raw_dimensions: set[str]
    # 上一可用结果：当前版本转换异常时保留它，避免页面直接空掉
    fallback_payload: dict[str, Any] | None = None
    fallback_version: int | None = None
    stale: bool = False
    conflict_rules: list[str] = field(default_factory=list)
    hits: int = 0


class VersionedResultCache:
    """按 key 保存下游结果，支持按维度定向失效和版本新鲜度判断。"""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self._items: dict[str, CachedResult] = {}

    def get(self, key: str, active_version: int) -> CachedResult | None:
        """读取缓存。只返回钉在当前生效映射版本上的结果；旧版本视为未命中。"""
        item = self._items.get(key)
        if item is None:
            return None
        if item.mapping_version != active_version:
            return None
        item.hits += 1
        return item

    def put(
        self,
        key: str,
        payload: dict[str, Any],
        mapping_version: int,
        raw_dimensions: set[str],
    ) -> CachedResult:
        previous = self._items.get(key)
        item = CachedResult(
            key=key,
            payload=payload,
            mapping_version=mapping_version,
            raw_dimensions=set(raw_dimensions),
            fallback_payload=previous.payload if previous else None,
            fallback_version=previous.mapping_version if previous else None,
        )
        self._items[key] = item
        return item

    def mark_stale(self, key: str, conflict_rules: list[str]) -> CachedResult | None:
        """转换异常时把当前结果标记为沿用旧值，并指出冲突规则。"""
        item = self._items.get(key)
        if item is None:
            return None
        item.stale = True
        item.conflict_rules = conflict_rules
        return item

    def invalidate_dimension(self, raw_dimension: str) -> list[str]:
        """仅失效包含指定原始维度的结果，返回被失效的 key 列表。"""
        removed = [
            key for key, item in self._items.items()
            if raw_dimension in item.raw_dimensions
        ]
        for key in removed:
            self._items.pop(key, None)
        return removed

    def invalidate_all(self) -> list[str]:
        removed = list(self._items)
        self._items.clear()
        return removed

    def keys_for_dimension(self, raw_dimension: str) -> list[str]:
        return [
            key for key, item in self._items.items()
            if raw_dimension in item.raw_dimensions
        ]

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "key": item.key,
                "mapping_version": item.mapping_version,
                "raw_dimensions": sorted(item.raw_dimensions),
                "stale": item.stale,
                "conflict_rules": item.conflict_rules,
                "hits": item.hits,
                "fallback_version": item.fallback_version,
            }
            for item in self._items.values()
        ]

    def __len__(self) -> int:
        return len(self._items)


def get_or_compute(
    cache: VersionedResultCache,
    key: str,
    active_version: int,
    raw_dimensions: set[str],
    loader: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], CachedResult | None, bool]:
    """统一的“读缓存 / 未命中则计算并回写”入口，返回 (结果, 缓存项, 是否命中)。"""
    hit = cache.get(key, active_version)
    if hit is not None:
        return hit.payload, hit, True
    payload = loader()
    item = cache.put(key, payload, active_version, raw_dimensions)
    return payload, item, False
