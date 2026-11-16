"""HTTP 级冒烟：映射命中、定向失效、单版本口径、异常保留全链路。"""
import json
import urllib.request

B = "http://127.0.0.1:8000/api"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        B + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


print("== 1. 数据集生成与幂等 ==")
d1 = call("POST", "/dimensionmap/dataset/generate")
print(d1["ok"], "|", d1["message"])
d2 = call("POST", "/dimensionmap/dataset/generate")
print(d2["ok"], "|", d2["message"])
assert d1["entry"]["映射版本"] == 1 and d2["entry"]["重复提交"] is True

print("== 2. 下钻首读（v1 正常，不误报 stale）==")
r1 = call("GET", "/drilldown/1/layered")
print("命中=", r1["缓存命中"], "版本=", r1["映射版本"], "stale=", r1["stale"], "|", r1["数据新鲜度"])
assert r1["缓存命中"] is False and r1["stale"] is False
r1b = call("GET", "/drilldown/1/layered")
print("再读命中=", r1b["缓存命中"])
assert r1b["缓存命中"] is True
r2 = call("GET", "/drilldown/2/layered")
r2b = call("GET", "/drilldown/2/layered")
print("路径2 再读命中=", r2b["缓存命中"], "依赖维度=", r2b["依赖原始维度"])

print("== 3. 聚合读取与单版本口径 ==")
a1 = call("GET", "/aggregate/1/layered")
a1b = call("GET", "/aggregate/1/layered")
versions = {layer["映射版本"] for layer in a1b["分层聚合结果"]}
print("粒度数=", len(a1b["分层聚合结果"]), "版本集合=", versions, "整体命中=", a1b["整体缓存命中"])
assert versions == {1} and a1b["整体缓存命中"] is True
call("GET", "/aggregate/2/layered")

print("== 4. 生效 channel 规则 -> 版本推进 + 定向失效 ==")
act = call("POST", "/dimensionmap/2/actions", {"values": {"action": "生效规则"}})
event = act["entry"]["_invalidation_event"]
print("规则版本=", act["entry"]["规则版本"], "新映射版本=", event["新映射版本"])
print("下钻失效=", event["定向失效结果"]["多维下钻"])
print("聚合失效=", sorted(event["定向失效结果"]["分层聚合"]))
assert event["定向失效结果"]["多维下钻"] == ["path:1"]
assert set(event["定向失效结果"]["分层聚合"]) == {
    "aggregate:1:grain:1", "aggregate:1:grain:2", "aggregate:1:grain:3"}

print("== 5. 失效后隔离性：路径1 重算，路径2 从未被失效 ==")
r1c = call("GET", "/drilldown/1/layered")
print("路径1 命中=", r1c["缓存命中"], "版本=", r1c["映射版本"])
assert r1c["缓存命中"] is False and r1c["映射版本"] == 2
# 路径2 在第 4 步失效事件中未出现，此处读取直接命中 v1 缓存项（版本口径切换后由缓存层判定重算）
r2c = call("GET", "/drilldown/2/layered")
print("路径2 依赖维度=", r2c["依赖原始维度"], "版本=", r2c["映射版本"])
assert "channel" not in r2c["依赖原始维度"] and r2c["映射版本"] == 2
# 聚合：AGGR-0002 不含 channel，失效事件未触碰；其缓存项一直保留
aggr_cache = call("GET", "/aggregate/cache")
print("聚合缓存现存=", sorted(i["key"] for i in aggr_cache["缓存明细"]))
assert {i["key"] for i in aggr_cache["缓存明细"]} >= {
    "aggregate:2:grain:1", "aggregate:2:grain:2"}

print("== 6. 调优先级触发失效并切换命中规则 ==")
p = call("POST", "/dimensionmap/3/priority", {"values": {"映射优先级": "5"}})
print(p["message"])
assert p["entry"]["_invalidation_event"] is not None
chain = call("GET", "/dimensionmap/chain")
print("当前映射版本=", chain["当前映射版本"], "失效事件数=", len(chain["失效链事件"]))

print("== 7. 转换异常保留上一可用结果（破坏 os_name 枚举逻辑）==")
before = call("GET", "/drilldown/1/layered")
os_layer = next(l for l in before["层级结果"] if l["原始维度名"] == "os_name")
print("破坏前 os_name 状态=", os_layer["转换状态"],
      "样本标准值=", [s["标准值"] for s in os_layer["取值样本"]])
assert os_layer["转换状态"] == "structural"
# 去掉 ios 映射：枚举表只剩 android，ios 样本将转换失败
upd = call("PUT", "/dimensionmap/5", {"values": {"值转换逻辑": "android=>安卓"}})
print(upd["message"])
assert upd["entry"]["_invalidation_event"] is not None
after = call("GET", "/drilldown/1/layered")
os_after = next(l for l in after["层级结果"] if l["原始维度名"] == "os_name")
print("破坏后 os_name 状态=", os_after["转换状态"],
      "冲突规则=", os_after["冲突规则"],
      "上一可用值=", os_after["上一可用标准值"])
print("路径新鲜度=", after["数据新鲜度"], "stale=", after["stale"])
assert os_after["转换状态"] == "convert_error"
assert "DIME-0005" in os_after["冲突规则"]
assert os_after["上一可用标准值"] == "iOS"
assert after["stale"] is True and "沿用" in after["数据新鲜度"]
# 新鲜度版本与当前引擎版本一致
chain_v = call("GET", "/dimensionmap/version")
assert after["映射版本"] == chain_v["active_mapping_version"]
print("异常场景端到端验证通过：保留 iOS、指出 DIME-0005、版本一致")

print("== 8. 草稿规则变更不触发失效 ==")
draft = call("POST", "/dimensionmap/6/priority", {"values": {"映射优先级": "1"}})
print(draft["message"])
assert "_invalidation_event" not in draft["entry"]

print()
print("HTTP 冒烟全部通过")
