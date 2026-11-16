# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 维度映射缓存与下游失效链

维度映射不再“一改优先级就清空全部下钻缓存”，命中、失效与聚合读取的链路如下：

```text
共享原始维度字段记录(dimensionfield，职责内只读)
  └─ MappingEngine.resolve(原始维度名[, 原始值])
       ├─ 生效规则按映射优先级排序，命中信息按 (原始维度名, 映射类型, 规则版本) 缓存
       ├─ 值转换：直映射 / 枚举转换 / 受控表达式；异常时保留上一可用结果并记录冲突规则
       └─ 命中快照带 命中规则编号 + 规则版本 + 映射版本，全程可追溯
            └─ 标准维度映射数据集（幂等单例，同版本重复提交不生成第二份）
                 └─ 下游 VersionedResultCache（多维下钻、分层聚合）
                      ├─ 规则生效/废弃/调优先级/改口径 → 规则版本+1、映射版本+1
                      ├─ 仅按原始维度名定向失效包含该维度的下钻路径与分层聚合粒度
                      └─ 下游只读取当前生效映射版本，单版本口径，不混版本聚合
```

业务边界取舍：规则变更顺序与映射版本高低冲突时，**版本一致性优先于缓存命中率**——
下游缓存项钉住映射版本，版本切换后旧版本一律视为未命中并重算回写，保证一份聚合结果
只来自一个映射版本。

相关接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/dimensionmap/raw-fields` | 共享原始维度字段记录（只读） |
| `POST /api/dimensionmap/dataset/generate` | 生成标准维度映射数据集（幂等） |
| `GET /api/dimensionmap/dataset/current` | 读取当前数据集 |
| `GET /api/dimensionmap/chain` | 命中缓存、下游缓存与失效链事件视图 |
| `GET /api/dimensionmap/version` | 当前映射版本与各规则版本水位 |
| `POST /api/dimensionmap/{id}/priority` | 调整映射优先级（生效规则触发定向失效） |
| `PUT /api/dimensionmap/{id}` | 更新规则口径（生效规则触发定向失效） |
| `GET /api/drilldown/{id}/layered` | 分层下钻结果（含命中规则、版本、新鲜度） |
| `GET /api/drilldown/cache` | 下钻结果缓存视图 |
| `GET /api/aggregate/{id}/layered` | 分层聚合结果（单版本口径） |
| `GET /api/aggregate/cache` | 分层聚合缓存视图 |

验证脚本在 `tests/`：`verify_mapping_chain.py`（服务层场景）与 `http_smoke.py`（HTTP 端到端），
均以 `python -I` 运行，不依赖第三方测试框架。

领域：数据工程
子方向：数据分析与数据应用
