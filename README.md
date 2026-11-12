# 航段时间清洗 · DST 安全入仓

面向航司数据工程师的网页工具：把地服导出的**当地时间**航段链解析为唯一 **UTC** 时间链，
避免夏令时（DST）切换时被错误入仓。支持地服与航司值班员两份更正的**三向合并**，
合并稿重跑整链清洗通过后再导出当地时间 CSV，供原流程使用。

## 快速启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>：

- 「① 单文件清洗」页签：点击「📦 载入内置夏令时样例」→「开始清洗」查看求解演示；
- 「② 更正合并入仓」页签：点击「📦 载入内置合并样例（三份）」→「开始合并比对」，
  处理冲突单元格后「重新核对」，观察合并后无法衔接的机尾如何被拦下。

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

自检（仅标准库，另需 Node 跑前端冒烟）：

```bash
python3 tests/test_solver.py       # 清洗求解器
python3 tests/test_merger.py       # 三向合并
node    tests/test_frontend_merge.js
```

## 输入 CSV 格式

UTF-8 编码，三份文件（底稿、地服更正、航司更正）沿用同一套清洗表头：

```csv
tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,duration_min_minutes,duration_max_minutes
B-7001,1,2026-10-25 00:30,Europe/London,2026-10-25 02:15,Europe/Berlin,90,130
```

| 列 | 含义 |
| --- | --- |
| `tail_number` | 机尾号（同一机尾构成一条航段链） |
| `seq` | 航段顺序，整数，同一机尾内唯一，按升序链接 |
| `dep_local` / `arr_local` | 起飞/落地当地时间，`YYYY-MM-DD HH:MM`（也接受 `T` 分隔与秒） |
| `dep_tz` / `arr_tz` | 两端 IANA 时区名，如 `Europe/Berlin`、`America/New_York` |
| `duration_min_minutes` / `duration_max_minutes` | 飞行时长下/上限（分钟，**闭区间**） |

## 求解规则

1. 当地时间 → UTC 候选：
   - 普通时刻 → 1 个候选；
   - **重复时刻**（秋季拨回重叠，如柏林 2026-10-25 02:30）→ 保留 **2 个候选**参与整链求解；
   - **不存在的时刻**（春季拨快空洞，如柏林 2026-03-29 02:30）→ 该行报错，整架机尾禁止导出。
2. 每个航段的 (起飞, 落地) 候选组合必须满足：飞行时长 ∈ `[下限, 上限]`（闭区间）。
3. 按 `seq` 链接同一机尾：下一段起飞 **不得早于** 上一段落地（相等允许）。
4. 判定：
   - **整链唯一解** → 允许导出该机尾的 UTC CSV；
   - **无解** → 展示冲突航段（前向扫描定位第一个无法衔接的航段及其候选），禁止导出；
   - **多解** → 展示全部可行候选链（最多展示 20 条），禁止导出；
   - **行级错误**（不存在时刻 / 非法时区 / 格式错误 / seq 重复等）→ 禁止导出。

## 三向合并规则（② 页签）

1. 上传**底稿 + 地服更正 + 航司值班员更正**三份 CSV，以 `tail_number + seq` 定位航段：
   - 每份文件内定位键不得重复；三份文件必须包含**同一组航段**，否则整体报错；
   - `tail_number / seq` 是定位键，不参与字段比对。
2. 对其余 6 个字段逐字段对照底稿：
   - 三方一致 → 沿用原值；
   - **仅一方改动**，或**双方改成同一值** → 自动合并（绿色标记）；
   - **双方改成不同值** → 冲突（红色标记），值班员从三份文件给出的值里**三选一**；
   - 自动合并的值同样可以改选（下拉框可覆盖为底稿/另一方的值）。
3. 点「重新核对」后，服务端按当前合并稿，仅对**任一方改过的机尾**重跑整链清洗，
   列出未通过的机尾及原因（无解冲突定位 / 多解 / 行级错误）。
4. 下载门槛（服务端强制复核，前端再兜底一次）：
   - 冲突单元格**全部选完**；
   - 所有被改过的机尾整链**唯一解**；
   - 任一条件不满足返回 409；**改选后必须重新核对**才能下载。
5. 下载产物 `merged_local.csv` 为合并后的**当地时间** CSV，表头与清洗输入一致，
   可直接交回原 `/api/solve` 流程入仓。

## 内置样例

### 单文件清洗 `samples/dst_sample.csv`

| 机尾 | 场景 | 期望结果 |
| --- | --- | --- |
| B-7001 | 欧洲拨回日三连段，重复时刻由时长区间+链约束消歧 | 唯一解，可导出 |
| B-7002 | 柏林 2026-03-29 02:30 不存在（拨快空洞） | 数据错误 |
| B-7003 | 第二段起飞早于第一段落地 | 无解，展示冲突航段 |
| B-7004 | 起降均为重复时刻，区间宽松 | 多解（2 条候选链） |
| B-7005 | 美国拨回日（纽约/洛杉矶/丹佛） | 唯一解，可导出 |

### 三向合并 `samples/merge/`（`merge_base / merge_ground / merge_airline.csv`）

| 机尾 | 合并情形 | 核对结果 |
| --- | --- | --- |
| B-8001 | 地服改第一段落地、航司改第二段起飞，各自单独成立 | 合并后第二段起飞（10:30Z）早于第一段落地（11:00Z），**无法衔接 → 无解，禁止下载** |
| B-8002 | 双方把落地时间改成不同值 → 唯一冲突单元格 | 三选一后整链唯一解 |
| B-8003 | 双方各改不同航段/字段，全部自动合并 | 整链唯一解 |
| B-8004 | 三方完全一致 | 不重跑清洗，不参与核对 |

> 两份更正 CSV 各自单独清洗时，四个机尾全部唯一解；只有合到一起才暴露 B-8001 的链断裂。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 网页入口 |
| `GET` | `/api/sample` | 下载内置清洗样例 CSV |
| `POST` | `/api/solve` | 上传 CSV（multipart 字段 `file`），返回逐机尾求解结果 JSON |
| `POST` | `/api/export` | 字段 `file` + `tail`（`__all` 表示全部可导出机尾），返回 UTC CSV；非唯一解 409 |
| `GET` | `/api/merge/sample/{kind}` | 下载合并样例，`kind ∈ base / ground / airline` |
| `POST` | `/api/merge/analyze` | 字段 `base / ground / airline` 三个 CSV，返回逐字段三向比对 JSON |
| `POST` | `/api/merge/verify` | 追加 `choices`（JSON 数组，每项 `{tail, seq, field, source}`），返回未决冲突与受影响机尾核对结果 |
| `POST` | `/api/merge/export` | 同样字段，复核通过后返回合并稿当地时间 CSV；未选完冲突或非唯一解 409 |
| `GET` | `/docs` | FastAPI 自动文档 |

## 目录结构

```
app/
  main.py            # FastAPI 路由（清洗 + 合并）
  solver.py          # 候选生成 + 整链求解 + 冲突定位（纯标准库）
  merger.py          # 三向比对 + 选值 + 合并稿清洗复核（纯标准库）
  static/index.html  # 单页前端（清洗 / 合并两个页签）
samples/
  dst_sample.csv
  merge/merge_base.csv / merge_ground.csv / merge_airline.csv
tests/
  test_solver.py / test_merger.py / test_frontend_merge.js
Dockerfile / docker-compose.yml / requirements.txt
```
