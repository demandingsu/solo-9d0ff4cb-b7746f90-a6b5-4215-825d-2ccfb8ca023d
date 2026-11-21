# 制造计划时序数据校正

FastAPI + SQLite + Docker Compose 的分层产线计划校正工具。计划分析员上传：

1. **层级映射**：`node_id, parent_id, level, name, weight`（产线 → 车间 → 工厂，单根树）
2. **产线预测**：`node_id, period, value`（每条产线每个时段一个值）
3. **已发布车间/工厂预测**：`node_id, period, value` 或 `level, period, value`

系统按层级重算总量、找出与已发布值不一致的时段，按节点权重最小化加权调整平方和，
生成各层数量一致的新快照；网页按节点展示每个时段的调整来源，结果可下载为 CSV。

发布快照后，分析员可在**快照页按选定时段分批上报实产 CSV**，系统合并整批后按车间/产线
规则对账，并逐项给出实产、校正前预测、校正后预测与覆盖产线，用同一组对账项分别求
校正前后的绝对误差之和。详见下文「实产上报与对账」。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

首次使用：新建数据集 → 分别上传 `sample_data/mapping.csv`、`sample_data/line_forecast.csv`、
`sample_data/published.csv` → 点击「运行校正」→ 检查「调整来源」→「发布此预览为快照」。
进入快照页即可按批次上报实产（示例文件见下文）。

本地不带 Docker 运行：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATABASE_PATH=./app.db uvicorn app.main:create_app --factory --port 8000
pip install -r requirements-dev.txt && pytest -q
```

## 校正模型

对每个时段独立求解。设产线值为决策变量 `x_l`，内部节点值恒等于其产线后代之和，
因此层级一致性 `父 = Σ子` 天然成立。目标函数为

```
Σ_节点 w_node · (x_node − base_node)²
```

其中 `base_node` 为上传的产线预测在节点上的加总。已发布值作为线性等式约束
（节点单一值，或某层全部节点的合计）。两者都是 `x_l` 的线性函数，于是问题化为
**带线性等式约束的加权最小二乘**，用零空间约化求解（numpy SVD + 线性方程组），
不可行时明确报错而不是悄悄违反发布值。

权重语义：权重越大越"不愿被调整"，代价按 `w·Δ²` 计入；因此同等条件下低权重节点承担
更多平移。权重为 0 的节点不参与代价（求解时用极小正则项保证结果唯一、可复现）。

### 校验与拒绝发布

上传映射时即校验并在不合规时**整次拒绝**：

- 映射成环（含自环）
- 同一节点多个父节点
- 父节点不存在
- 缺层：根不是 plant / 车间下没有产线 / 层级为空 / 层级与树深度不符 / 多根
- 使用已发布预测但节点或层级不存在、同一节点同一时段两个不同发布值
- 父节点与其全部子节点的已发布值互相矛盾（预先给出可读错误）
- 约束矩阵不相容（例如工厂合计与车间合计冲突）→ `OptimizationError`

### 版本、预览与失败语义

- 每个数据集有单调递增的 `input_version`；上传内容有变化才 +1。
- 预览记录输入版本 + 三个文件的 sha256。发布时在同一个写事务内重新校验版本与哈希，
  任何一项不匹配 → `StalePreviewError`，**旧预览不得发布**。
- 校正失败不写入 preview 也不写快照；上一版已发布快照原样保留。
- 快照不可变：每次发布新建版本并记录父快照，旧版本可继续查看/下载。
- 上传 / 预览 / 发布在同一 SQLite 写事务中完成（`BEGIN IMMEDIATE`），
  并发上传与预览不会混用版本。

## 页面

- `/` 数据集列表
- `/datasets/{id}` 上传、校正、发布、历史快照、操作记录
- `/datasets/{id}/snapshots/{v}` 只读快照页，同时是实产上报与对账页
- 每个时段折叠面板显示：不一致时段明细（基线 vs 已发布 vs 差额）、已发布约束、
  按节点的调整来源（`直接发布` / `层级总量` / `层级平衡`）、产线级溯源、校正后各层数值
- 快照页每个时段另有一个「实产上报与对账」面板：文件上传、误差汇总（同一组对账项的校正前/后
  绝对误差之和）、对账明细（实产 / 两套预测 / 覆盖产线 / 两项误差）、车间状态、缺数产线与批次记录
- 下载：`/datasets/{id}/download/snapshot.csv`、`adjustments.csv`、`conflicts.csv`
  （带 `?version=N` 或 `?preview=N`）
- 实产对账 CSV：`/api/datasets/{id}/snapshots/{v}/actuals.csv[?period=YYYY-MM]`

## 实产上报与对账

在已发布快照页选定时段，分批上传实产 CSV：

```csv
node_id,quantity
S1,300
L1,120
```

- 列：`node_id, quantity`（也接受 `value` / `qty` / `actual`），编号是车间或产线，
  数量必须是非负整数；未知编号、工厂节点、负数或小数整批拒绝。
- 同一编号可在后续批次更正；同一批次里重复行以最后一行为准。
- **先合并整批再核对**：新批次与已存实产合并成一个完整状态后统一判断，
  不会出现只看到半批数据的中间态。
- 已报实产的产线单独对账：实产 vs 该产线预测。
- 车间报了总量时，扣掉已报产线实产得到余额，余额与该车间**未报产线预测合计**对账，
  并在明细里列出这些被覆盖的产线。
- 车间没报总量时，其未报产线标为**缺数**，既不参与明细也不计入误差汇总。
- **整批退回**：任一车间余额为负，或产线已报齐但实产合计与车间总量不符时，
  整批不落库，返回冲突车间列表；已有实产和对账结果原样保留。
- **误差汇总**：对同一组对账项分别计算 `Σ|实产 − 校正前预测|` 与
  `Σ|实产 − 校正后预测|`；补报或更正成功后，明细与汇总在同一事务里一起更新。
- 所有实产按「数据集 + 快照版本 + 时段」存放，旧快照继续显示各自当时的实产，
  互不覆盖。每次成功/被退回的批次都会留一条记录。

示例文件（使用 `sample_data/` 的映射与预测，快照的 `2026-01` 时段）：

| 文件 | 内容 | 结果 |
| --- | --- | --- |
| `actuals_2026-01_batch1.csv` | S1 总量 300、L1 实产 120 | 2 个对账项，L3/L4 缺数 |
| `actuals_2026-01_batch2.csv` | L2=180、L3=95 | L2 补报，S1 产线报齐且合计 300 一致；L4 缺数 |
| `actuals_2026-01_batch3_correction.csv` | L3=88 | 更正 L3，误差汇总同步更新 |
| `actuals_2026-01_conflict_negative.csv` | S2 总量 80 | 退回：余额为负（S2 已报 88 > 80） |
| `actuals_2026-01_conflict_mismatch.csv` | S1 总量 250 | 退回：S1 产线报齐但合计 300 ≠ 250 |

## JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/datasets` | 新建数据集，body `{"name": "..."}` |
| GET | `/api/datasets/{id}` | 上传状态、预览、快照、事件 |
| POST | `/api/datasets/{id}/upload?kind=mapping\|line_forecast\|published` | multipart `file` |
| POST | `/api/datasets/{id}/preview` | 运行校正 |
| POST | `/api/datasets/{id}/publish` | body `{"preview_id": N}` |
| GET | `/api/datasets/{id}/snapshots` | 快照列表 |
| GET | `/api/datasets/{id}/snapshots/{v}` | 快照详情 |
| POST | `/api/datasets/{id}/snapshots/{v}/actuals?period=YYYY-MM` | multipart `file`，合并实产批次 |
| GET | `/api/datasets/{id}/snapshots/{v}/actuals[?period=YYYY-MM]` | 实产对账结果与批次记录 |
| GET | `/api/datasets/{id}/snapshots/{v}/actuals.csv[?period=YYYY-MM]` | 对账明细 CSV |
| GET | `/healthz` | 健康检查 |

错误响应：`{"error": "...", "code": "stale_preview_error"}`，HTTP 400。
实产批次被退回时 `code` 为 `actuals_validation_error`，并附带
`details.conflicts`（冲突车间、原因、总量、已报合计、余额）。

## 测试

```bash
pytest -q
```

覆盖：映射成环/多父/缺层、CSV 解析、约束可行与不可行、权重变化对调整分配的影响、
随机树与独立 KKT 解交叉验证、版本失效与旧预览拒绝发布、失败保留上一版、并发、
HTTP 页面与下载，以及实产上报的补报/更正、余额拆分、缺数标记、整批退回与并发。

## 目录

```
app/
  main.py        FastAPI 路由（页面 + JSON API）
  service.py     上传/预览/发布/实产批次编排、版本与失效判定、CSV 导出
  reconcile.py   加权最小二乘校正与一致性校验
  actuals.py     实产批次合并后的对账规则与误差汇总（纯函数）
  hierarchy.py   树构建与结构校验
  csv_io.py      CSV 解析
  db.py          SQLite schema 与事务
  templates/     Jinja2 页面
  static/        样式
tests/           105 项自动化测试
sample_data/     可直接上传的示例文件（含成环反例与实产批次/冲突样例）
```

## 限制

- 单进程 SQLite；水平扩展需要换成网络数据库并加分布式锁/乐观版本。
- 已发布 `line` 层级合计会锁定每条产线（合计拆分规则未定义，不擅自猜测）。
- 实产对账当前不做新的层级优化：校正后预测直接取自已发布快照；误差汇总只是比较两套既有预测。
- 实产按批次留痕，但不支持删除/撤销单个编号的实产；如需清零只能再报一个新值。
- 不保存用户账号与权限；如需多人隔离应在反向代理层加认证。
- 不支持按周/季度等非 ISO 周期的自然排序（按字符串前缀解析年月）。
- 当前沙箱内没有 Docker 守护进程，`Dockerfile` / `docker-compose.yml` 已写好且 compose 配置可解析，
  但未实际构建镜像；应用已在本地以 uvicorn 启动，并完成上传/校正/发布/实产分批补报与更正/
  两类批次退回/下载全链路手工验证。
