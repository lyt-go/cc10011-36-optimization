"""维度映射解析引擎：规则版本、命中快照缓存与下游定向失效链。

链路（对应需求“先梳理映射命中、缓存失效到聚合读取的链路”）：

    原始维度字段记录(共享只读)
        │  MappingEngine.resolve(原始维度名, 原始值)
        ▼
    生效规则按映射优先级排序 ── 命中 ──▶ 命中快照
        │  快照 key = (原始维度名, 映射类型, 规则版本)，稳定命中直接复用
        ▼
    值转换（直映射/枚举/表达式）── 异常 ──▶ 保留上一可用结果 + 记录冲突规则
        ▼
    标准维度映射数据集（幂等单例，重复提交不生成第二份）
        │
        ▼
    下游 VersionedResultCache（多维下钻 / 分层聚合）
        │  规则生效/废弃/调优先级 → 规则版本+1，仅按原始维度名定向失效
        ▼
    下游读取：只接受当前生效映射版本（单一版本口径，不混版本）

业务边界取舍：规则变更顺序与版本高低冲突时，下游聚合只读取“当前生效版本”，
旧版本缓存即使还在也视为未命中、整体重算并回写——版本一致性优先于缓存命中率。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.mapping_cache import VersionedResultCache
from app.store import store

MODULE = "dimensionmap"
RAW_FIELD_MODULE = "dimensionfield"
EFFECTIVE_STATUS = "已生效"
DIRECT_TYPE = "直映射"
ENUM_TYPE = "枚举转换"
EXPR_TYPE = "表达式转换"
SUPPORTED_TYPES = {DIRECT_TYPE, ENUM_TYPE, EXPR_TYPE}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _priority(row: dict[str, Any]) -> int:
    """映射优先级：数值越小越优先；解析失败排到最后。"""
    raw = str(row.get("映射优先级") or "").strip()
    match = re.search(r"-?\d+", raw)
    return int(match.group()) if match else 10_000


@dataclass
class HitSnapshot:
    """一条原始维度记录的命中快照：命中规则与映射版本全程可追溯。"""

    raw_dimension: str
    raw_value: Any
    standard_dimension: str
    standard_value: Any
    mapping_type: str
    rule_id: str
    rule_row_id: int | None
    rule_version: int
    engine_version: int
    used_default: bool
    convert_status: str          # ok / convert_error / no_rule
    conflict_rules: list[str] = field(default_factory=list)
    fallback_value: Any = None
    fallback_version: int | None = None
    resolved_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "原始维度名": self.raw_dimension,
            "原始值": self.raw_value,
            "标准维度名": self.standard_dimension,
            "标准值": self.standard_value,
            "映射类型": self.mapping_type,
            "命中规则编号": self.rule_id,
            "规则版本": self.rule_version,
            "映射版本": self.engine_version,
            "使用默认取值": self.used_default,
            "转换状态": self.convert_status,
            "冲突规则": self.conflict_rules,
            "上一可用标准值": self.fallback_value,
            "上一可用版本": self.fallback_version,
            "解析时间": self.resolved_at,
        }


class MappingEngine:
    """映射解析、快照缓存、版本推进与下游失效联动的统一入口。"""

    def __init__(self) -> None:
        # 当前生效的引擎（全局）映射版本：规则每次生效/废弃/调优先级时 +1
        self.active_version = 1
        # 命中缓存：key = (原始维度名, 映射类型, 规则版本)，只缓存“命中了哪条规则”
        # 这一维度级、与取值无关的信息；值转换随每次输入执行，复用同一条命中规则
        self._hits: dict[tuple[str, str, int], dict[str, Any]] = {}
        # 最近一次转换成功的标准值：转换异常时作为上一可用结果保留
        self._last_good: dict[str, HitSnapshot] = {}
        # 下游结果缓存注册：多维下钻、分层聚合等模块在这里登记
        self._consumers: dict[str, VersionedResultCache] = {}
        # 失效链事件日志，便于复核“哪条规则变更影响了哪些下游结果”
        self._events: list[dict[str, Any]] = []
        # 标准维度映射数据集（幂等单例）
        self._dataset: dict[str, Any] | None = None
        # 数据集生成时钉住的版本；版本推进后数据集可重新生成（同版本重复提交直接复用）
        self._dataset_version: int | None = None

    # ------------------------------------------------------------------ 注册
    def register_consumer(self, name: str, cache: VersionedResultCache) -> None:
        """下游模块注册自己的结果缓存，失效链按注册名联动。"""
        self._consumers[name] = cache

    # ------------------------------------------------------------- 规则读取
    def _active_rules(self, raw_dimension: str | None = None) -> list[dict[str, Any]]:
        rows = [
            row for row in store.rows(MODULE)
            if row.get("status") == EFFECTIVE_STATUS
        ]
        if raw_dimension is not None:
            rows = [row for row in rows if row.get("原始维度名") == raw_dimension]
        # 按映射优先级升序，稳定排序保证同优先级可复现
        return sorted(rows, key=lambda row: (_priority(row), int(row.get("id", 0))))

    def candidate_rules(self, raw_dimension: str) -> list[dict[str, Any]]:
        """同一原始维度名上的全部候选规则（含优先级），用于冲突复核。"""
        return self._active_rules(raw_dimension)

    # ------------------------------------------------------------- 值转换
    @staticmethod
    def _parse_enum_logic(logic: str) -> dict[str, str]:
        """枚举转换逻辑支持 'a=>b, c=>d' 与 'a:b;c:d' 两种写法。"""
        table: dict[str, str] = {}
        for piece in re.split(r"[,，;；]", str(logic or "")):
            piece = piece.strip()
            if not piece:
                continue
            matched = re.split(r"=>|->|[:：]", piece, maxsplit=1)
            if len(matched) == 2:
                table[matched[0].strip()] = matched[1].strip()
        return table

    def _convert(
        self,
        rule: dict[str, Any],
        raw_value: Any,
    ) -> tuple[Any, bool]:
        """按映射类型转换单值，返回 (转换结果, 是否异常)。"""
        mapping_type = str(rule.get("映射类型") or DIRECT_TYPE).strip()
        logic = str(rule.get("值转换逻辑") or "").strip()
        text = "" if raw_value is None else str(raw_value)
        try:
            if mapping_type == ENUM_TYPE:
                table = self._parse_enum_logic(logic)
                if text not in table:
                    return None, True
                return table[text], False
            if mapping_type == EXPR_TYPE:
                # 受控表达式：只允许 upper/lower/trim/前缀/替换 这几种白名单写法，
                # 不做任意代码求值，避免表达式注入。
                expr = logic.lower().replace(" ", "")
                if expr in {"upper()", "upper"}:
                    return text.upper(), False
                if expr in {"lower()", "lower"}:
                    return text.lower(), False
                if expr in {"trim()", "trim"}:
                    return text.strip(), False
                prefix = re.match(r"prefix\('([^']*)'\)", expr)
                if prefix:
                    return f"{prefix.group(1)}{text}", False
                replaced = re.match(r"replace\('([^']*)','([^']*)'\)", expr)
                if replaced:
                    return text.replace(replaced.group(1), replaced.group(2)), False
                return None, True
            # 直映射：取值原样透传，标准维度名由规则给出
            return raw_value, False
        except (ValueError, TypeError, AttributeError):
            return None, True

    # ------------------------------------------------------------- 命中解析
    def _hit_rule(self, raw_dimension: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """按优先级返回该维度的命中规则与候选规则列表，命中信息按三元组缓存复用。"""
        rules = self._active_rules(raw_dimension)
        if not rules:
            return None, []
        rule = rules[0]
        mapping_type = str(rule.get("映射类型") or DIRECT_TYPE).strip()
        rule_version = int(rule.get("规则版本", 1))
        hit_key = (raw_dimension, mapping_type, rule_version)
        hit = self._hits.get(hit_key)
        if hit is None:
            hit = {"rule": rule, "rule_version": rule_version, "mapping_type": mapping_type}
            self._hits[hit_key] = hit
        return hit["rule"], rules

    def resolve(
        self,
        raw_dimension: str,
        raw_value: Any = None,
    ) -> HitSnapshot:
        """解析单个原始维度，返回带规则编号与版本的命中快照。

        - 命中了哪条规则按 (原始维度名, 映射类型, 规则版本) 缓存，稳定命中直接复用；
        - 值转换随输入执行；转换异常时保留上一可用结果，并指出冲突规则；
        - 结构性解析（raw_value 为 None，例如下钻分层）不做值转换，只返回命中口径；
        - 没有任何生效规则时回退为未匹配并标记 no_rule。
        """
        rule, rules = self._hit_rule(raw_dimension)
        if rule is None:
            return self._no_rule_snapshot(raw_dimension, raw_value)

        mapping_type = str(rule.get("映射类型") or DIRECT_TYPE).strip()
        rule_version = int(rule.get("规则版本", 1))
        standard_dimension = str(rule.get("标准维度名") or "").strip()
        conflict_rules = [str(item.get("规则编号")) for item in rules[1:]]
        common = {
            "raw_dimension": raw_dimension,
            "raw_value": raw_value,
            "standard_dimension": standard_dimension,
            "mapping_type": mapping_type,
            "rule_id": str(rule.get("规则编号")),
            "rule_row_id": rule.get("id"),
            "rule_version": rule_version,
            "engine_version": self.active_version,
        }

        # 结构性解析：下游只关心“该维度映射到哪个标准维度、命中哪条规则”
        if raw_value is None:
            return HitSnapshot(
                **common,
                standard_value=None,
                used_default=False,
                convert_status="structural",
                conflict_rules=conflict_rules,
                resolved_at=_now(),
            )

        converted, errored = self._convert(rule, raw_value)
        if not errored:
            snapshot = HitSnapshot(
                **common,
                standard_value=converted,
                used_default=False,
                convert_status="ok",
                conflict_rules=conflict_rules,
                resolved_at=_now(),
            )
            self._last_good[raw_dimension] = snapshot
            return snapshot

        # 转换异常：保留上一可用结果；从未成功过则回退默认取值
        previous = self._last_good.get(raw_dimension)
        return HitSnapshot(
            **common,
            standard_value=(
                previous.standard_value if previous is not None
                else rule.get("默认取值")
            ),
            used_default=previous is None,
            convert_status="convert_error",
            conflict_rules=[str(rule.get("规则编号")), *conflict_rules],
            fallback_value=previous.standard_value if previous else None,
            fallback_version=previous.rule_version if previous else None,
            resolved_at=_now(),
        )

    def resolve_dimension_samples(self, raw_dimension: str) -> list[HitSnapshot]:
        """基于共享原始维度字段记录，解析某维度的全部取值样本。

        供下钻/聚合页面在结构性命中之外，顺带暴露取值转换健康度；
        样本解析同样走命中缓存与异常保留逻辑。
        """
        samples = [
            row for row in store.rows(RAW_FIELD_MODULE)
            if str(row.get("原始维度名")) == raw_dimension
        ]
        return [
            self.resolve(raw_dimension, row.get("原始值"))
            for row in samples
        ]

    def _no_rule_snapshot(self, raw_dimension: str, raw_value: Any) -> HitSnapshot:
        return HitSnapshot(
            raw_dimension=raw_dimension,
            raw_value=raw_value,
            standard_dimension=raw_dimension,
            standard_value=raw_value,
            mapping_type="未匹配",
            rule_id="",
            rule_row_id=None,
            rule_version=0,
            engine_version=self.active_version,
            used_default=True,
            convert_status="no_rule",
            resolved_at=_now(),
        )

    # ------------------------------------------------------------- 版本推进
    def bump_on_rule_change(
        self,
        rule: dict[str, Any],
        change_type: str,
    ) -> dict[str, Any]:
        """规则生效/废弃/调优先级后的统一入口：规则版本+1、引擎版本+1、定向失效。

        只失效包含该原始维度名的下钻路径与分层聚合结果，其它维度缓存保持不动；
        命中快照按规则版本自然失效（旧版本 key 不再被命中），不清空整张快照表。
        """
        raw_dimension = str(rule.get("原始维度名") or "").strip()
        rule["规则版本"] = int(rule.get("规则版本", 1)) + 1
        rule["最近更新时间"] = _now()
        previous_version = self.active_version
        self.active_version += 1
        # 命中缓存钉的是规则版本：版本推进后旧三元组不再成立，整体丢弃并按新规则重建；
        # 下游结果缓存不在此清空，而是按原始维度名定向失效
        self._hits.clear()

        affected: dict[str, list[str]] = {}
        for name, cache in self._consumers.items():
            affected[name] = cache.invalidate_dimension(raw_dimension)

        event = {
            "时间": _now(),
            "变更类型": change_type,
            "规则编号": rule.get("规则编号"),
            "原始维度名": raw_dimension,
            "规则版本": rule["规则版本"],
            "旧映射版本": previous_version,
            "新映射版本": self.active_version,
            "定向失效结果": affected,
            "失效维度数": 1,
        }
        self._events.append(event)
        return event

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(reversed(self._events[-limit:]))

    def consumer_snapshot(self) -> dict[str, Any]:
        return {
            name: {
                "缓存条数": len(cache),
                "明细": cache.snapshot(),
            }
            for name, cache in self._consumers.items()
        }

    # ------------------------------------------------------------- 映射数据集
    def generate_dataset(self) -> dict[str, Any]:
        """读取原始维度字段记录，生成标准维度映射数据集。

        幂等单例：同一映射版本内重复提交直接返回既有数据集，不生成第二份；
        版本推进后再次生成才产出新版本数据集，旧版本号在数据集里可追溯。
        """
        if self._dataset is not None and self._dataset_version == self.active_version:
            self._dataset["重复提交"] = True
            return self._dataset

        raw_rows = store.rows(RAW_FIELD_MODULE)
        items: list[dict[str, Any]] = []
        for row in raw_rows:
            snapshot = self.resolve(
                str(row.get("原始维度名")),
                row.get("原始值"),
            )
            item = snapshot.to_dict()
            item["字段记录编号"] = row.get("字段编号")
            item["来源表"] = row.get("来源表")
            items.append(item)

        self._dataset = {
            "数据集编号": "STANDARD_DIMENSION_DATASET",
            "映射版本": self.active_version,
            "生成时间": _now(),
            "记录数": len(items),
            "重复提交": False,
            "items": items,
        }
        self._dataset_version = self.active_version
        return self._dataset

    def current_dataset(self) -> dict[str, Any] | None:
        return self._dataset

    def chain_overview(self) -> dict[str, Any]:
        """映射命中 → 缓存失效 → 聚合读取的全链路视图。"""
        return {
            "当前映射版本": self.active_version,
            "命中缓存条数": len(self._hits),
            "命中缓存": [
                {
                    "原始维度名": key[0],
                    "映射类型": key[1],
                    "规则版本": key[2],
                    "命中规则编号": hit["rule"].get("规则编号"),
                    "标准维度名": hit["rule"].get("标准维度名"),
                }
                for key, hit in self._hits.items()
            ],
            "上一可用结果": [
                snap.to_dict() for snap in self._last_good.values()
            ],
            "下游缓存": self.consumer_snapshot(),
            "数据集版本": self._dataset_version,
            "失效链事件": self.events(),
        }


# 进程内单例：下游模块与路由都通过 mapping_engine 访问，保证口径唯一
mapping_engine = MappingEngine()
