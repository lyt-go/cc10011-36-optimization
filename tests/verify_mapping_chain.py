"""维度映射缓存与失效链的服务层验证脚本（不依赖 HTTP）。

覆盖场景：
1. 标准维度映射数据集生成 + 幂等（重复提交不生成第二份）
2. 命中快照含规则编号与规则/映射版本，可追溯
3. 稳定命中复用、优先级排序生效
4. 规则变更只定向失效包含该维度的下钻/聚合缓存，其它维度保持命中
5. 转换异常保留上一可用结果并指出冲突规则
6. 下游只读取当前生效映射版本（单版本口径）
7. 调优先级触发定向失效；草稿规则变更不影响下游
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/workspace/backend")

from app.services.aggregate import AggregateService, aggregate_cache
from app.services.dimensionmap import DimensionmapService
from app.services.drilldown import DrilldownService, drilldown_cache
from app.services.mapping_engine import mapping_engine
from app.store import store

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -> {detail}" if detail else ""))
    if not condition:
        failures.append(name)


dmap = DimensionmapService()
drill = DrilldownService()
aggr = AggregateService()

# ------------------------------------------------ 1. 数据集生成与幂等
dataset_1 = dmap.generate_dataset()
check("数据集生成返回 7 条原始字段记录", dataset_1["记录数"] == 7, str(dataset_1["记录数"]))
check("数据集钉住映射版本 v1", dataset_1["映射版本"] == 1)
first_row = dataset_1["items"][0]
check("数据集行带命中规则编号", first_row["命中规则编号"] == "DIME-0001", str(first_row))
check("数据集行带规则版本与映射版本", first_row["规则版本"] == 1 and first_row["映射版本"] == 1)

dataset_2 = dmap.generate_dataset()
check("重复提交不生成第二份（同一对象/同一版本）", dataset_2 is dataset_1 and dataset_2["重复提交"] is True)

# channel=app 应按优先级 10 的 DIME-0002 命中 -> 移动端
channel_rows = [row for row in dataset_1["items"] if row["原始维度名"] == "channel"]
app_row = next(row for row in channel_rows if row["原始值"] == "app")
check("优先级排序：channel/app 命中 DIME-0002 -> 移动端",
      app_row["命中规则编号"] == "DIME-0002" and app_row["标准值"] == "移动端", str(app_row))
check("同维度次优规则进入冲突规则列表", "DIME-0003" in app_row["冲突规则"], str(app_row["冲突规则"]))
check("表达式转换：region/east -> EAST",
      next(row for row in dataset_1["items"] if row["原始维度名"] == "region" and row["原始值"] == "east")["标准值"] == "EAST")

# ------------------------------------------------ 2. 下钻/聚合缓存
r1, err = drill.layered_result(1)
assert err is None
check("下钻首读未命中缓存", r1["缓存命中"] is False)
check("下钻结果钉住 v1 且新鲜度展示版本", r1["映射版本"] == 1 and "v1" in r1["数据新鲜度"], r1["数据新鲜度"])
r1_again, _ = drill.layered_result(1)
check("下钻稳定命中复用缓存", r1_again["缓存命中"] is True)

r2, _ = drill.layered_result(2)  # region -> channel
check("第二条下钻路径独立缓存", r2["缓存命中"] is False)
drill.layered_result(2)
check("第二条路径二次读取命中", drill.layered_result(2)[0]["缓存命中"] is True)

a1, err = aggr.layered_aggregate(1)
assert err is None
check("聚合首读未命中", a1["整体缓存命中"] is False)
a1_again, _ = aggr.layered_aggregate(1)
check("聚合二次读取全部粒度命中", a1_again["整体缓存命中"] is True)
check("聚合结果只钉一个映射版本", {layer["映射版本"] for layer in a1_again["分层聚合结果"]} == {1})
# 先读取 AGGR-0002（region、user_id/region，均不含 channel），用于验证定向失效的隔离性
a2, err = aggr.layered_aggregate(2)
assert err is None
check("第二份聚合规则独立缓存", a2["整体缓存命中"] is False)

snapshot_before = mapping_engine.consumer_snapshot()
check("两个下游消费方都已注册", set(snapshot_before) == {"多维下钻", "分层聚合"})

# ------------------------------------------------ 3. 定向失效：改 channel 规则（规则 id=2 生效动作推进版本）
rule_channel = store.find("dimensionmap", 2)
_, msg, event = dmap.run_action(2, "生效规则")
check("channel 规则版本推进到 v2", rule_channel["规则版本"] == 2, msg)
check("引擎映射版本推进到 v2", mapping_engine.active_version == 2)
check("失效事件只报告 1 个维度", event["失效维度数"] == 1)
affected_drill = event["定向失效结果"]["多维下钻"]
check("仅含 channel 的下钻路径 1 被失效，路径 2 保留",
      affected_drill == ["path:1"], str(event["定向失效结果"]))
affected_aggr = event["定向失效结果"]["分层聚合"]
# AGGR-0001 三个粒度（channel / region,channel / channel,region,os_name）都含 channel；
# AGGR-0002（region / user_id,region）不含 channel，应保留
check("含 channel 的聚合粒度被定向失效，其它粒度保留",
      set(affected_aggr) == {"aggregate:1:grain:1", "aggregate:1:grain:2", "aggregate:1:grain:3"},
      str(affected_aggr))
check("下钻缓存仅失效 path:1，path:2 缓存仍在",
      set(item["key"] for item in drilldown_cache.snapshot()) == {"path:2"})
check("不含 channel 的 AGGR-0002 缓存仍然保留",
      {item["key"] for item in aggregate_cache.snapshot()} == {"aggregate:2:grain:1", "aggregate:2:grain:2"})

r1_after, _ = drill.layered_result(1)
r2_after, _ = drill.layered_result(2)
check("路径1 版本切换后未命中并重算为 v2", r1_after["缓存命中"] is False and r1_after["映射版本"] == 2)
# 路径2（region/user_id）不依赖 channel：定向失效没有触碰它，但版本口径切换后读取会重算回写
r2_hit = drill.layered_result(2)[0]
check("路径2 重算后仍为 v2 且依赖维度不含 channel",
      r2_hit["映射版本"] == 2 and "channel" not in r2_hit["依赖原始维度"],
      str(r2_hit["依赖原始维度"]))

a1_after, _ = aggr.layered_aggregate(1)
grain1 = a1_after["分层聚合结果"][0]  # channel 单粒度 -> 曾被失效
check("聚合受影响粒度重算且版本为 v2", grain1["映射版本"] == 2 and grain1["缓存命中"] is False)

# ------------------------------------------------ 4. 单版本口径：所有下游结果只属于当前版本
check("下钻全部结果版本统一为 v2",
      all(layer["映射版本"] == 2 for layer in drill.layered_result(1)[0]["层级结果"]))

# ------------------------------------------------ 5. 转换异常保留上一可用结果
# 先制造一次成功解析 os_name=ios
ok_snap = mapping_engine.resolve("os_name", "ios")
check("os_name/ios 正常命中 DIME-0005 -> iOS",
      ok_snap.convert_status == "ok" and ok_snap.standard_value == "iOS", ok_snap.to_dict())
# 把枚举逻辑改坏（去掉 ios 映射），再推进版本
rule_os = store.find("dimensionmap", 5)
rule_os["值转换逻辑"] = "android=>安卓"
_, _, os_event = dmap.run_action(5, "生效规则")
check("os_name 变更后映射版本推进到 v3", mapping_engine.active_version == 3)
check("os_name 变更只失效含 os_name 的路径1 与聚合 g3",
      os_event["定向失效结果"]["多维下钻"] == ["path:1"]
      and os_event["定向失效结果"]["分层聚合"] == ["aggregate:1:grain:3"],
      str(os_event["定向失效结果"]))
# 不含 os_name 的 g1/g2 缓存保留
check("不含 os_name 的聚合粒度在 v3 失效后保留",
      {item["key"] for item in aggregate_cache.snapshot()} >= {"aggregate:1:grain:1", "aggregate:1:grain:2"})
bad_snap = mapping_engine.resolve("os_name", "ios")
check("转换异常状态为 convert_error 且指出冲突规则 DIME-0005",
      bad_snap.convert_status == "convert_error" and "DIME-0005" in bad_snap.conflict_rules,
      bad_snap.to_dict())
check("异常时保留上一可用结果 iOS 与上一版本",
      bad_snap.standard_value == "iOS" and bad_snap.fallback_value == "iOS" and bad_snap.fallback_version is not None,
      bad_snap.to_dict())
r1_v3, _ = drill.layered_result(1)
os_layer = next(layer for layer in r1_v3["层级结果"] if layer["原始维度名"] == "os_name")
check("下钻页面新鲜度与实际版本一致并提示沿用旧值",
      r1_v3["映射版本"] == 3 and "沿用" in r1_v3["数据新鲜度"] and os_layer["转换状态"] == "convert_error",
      r1_v3["数据新鲜度"])

# ------------------------------------------------ 6. 调优先级触发失效；草稿不触发
rule_channel_2 = store.find("dimensionmap", 3)  # channel 次优规则，已生效
_, p_msg, p_event = dmap.adjust_priority(3, "5")
check("调优先级后 DIME-0003 变为 v4 版本引擎", mapping_engine.active_version == 4 and p_event is not None, p_msg)
new_hit = mapping_engine.resolve("channel", "app")
check("优先级调整后 channel/app 改命中 DIME-0003",
      new_hit.rule_id == "DIME-0003" and new_hit.standard_value == "APP渠道", new_hit.to_dict())

draft_rule = store.find("dimensionmap", 6)  # 草稿 event_name
_, draft_msg, draft_event = dmap.adjust_priority(6, "1")
check("草稿规则调优先级不推进版本、不触发失效", mapping_engine.active_version == 4 and draft_event is None, draft_msg)
_, _, draft_action_event = dmap.run_action(6, "提交规则评审")
check("草稿->评审中不触发失效", draft_action_event is None)

# ------------------------------------------------ 7. 链路视图可追溯
chain = dmap.chain_overview()
check("链路视图含命中缓存、下游缓存、失效事件",
      len(chain["命中缓存"]) > 0 and len(chain["失效链事件"]) >= 3 and "下游缓存" in chain)
check("失效事件按时间倒序可追溯规则号与版本",
      chain["失效链事件"][0]["规则编号"] == "DIME-0003")

# ------------------------------------------------ 8. 原始字段只读：服务层无写入口
check("原始字段记录只暴露只读列表", hasattr(dmap, "list_raw_fields") and not hasattr(dmap, "create_raw_field"))

print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全部验证通过")
