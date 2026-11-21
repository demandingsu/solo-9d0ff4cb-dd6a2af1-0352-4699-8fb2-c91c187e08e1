# 预制构件称重 · 拒收规则试算平台

数据员在称重明细接入数仓前，用本平台**试出每条拒收规则的触发数据**：录入带编号的规则和各列整数取值范围，
系统为每条规则求解一行仅触发该规则的称重 CSV；做不到时区分“范围内从不触发”和“只能连带触发”，
并支持下载 CSV、回放核对实际触发的规则编号，供入仓回归使用。

## 功能

- **规则录入**：每行一条 `编号 | 比较式1；比较式2…`，规则内所有比较同时成立（AND）。
  - 字段：`毛重 / 托盘重 / 申报净重`（别名 `G / T / D`、`gross / tare / declared`）。
  - 比较两边可写字段、非负整数及它们的加减式，如 `毛重 - 托盘重 - 申报净重 > 500`。
  - 比较符：`= == != <> > >= < <=`；分隔符：`； ; , 且 and`；`#` 开头为注释。
- **取值范围**：毛重、托盘重、申报净重各自填写整数闭区间，允许值 0 ~ 1,000,000 克。
- **逐规则求解**（基于 z3 SMT），三种结论：
  | 结论 | 含义 |
  |---|---|
  | 可单独触发（solo） | 存在一行只触发该规则，直接给出该行 |
  | 只能连带触发（joint） | 规则可触发，但任何触发它的行都会连带其他规则；给出**连带规则数最少**的一行及全部触发编号 |
  | 范围内从不触发（never） | 规则自身在给定取值范围内不可满足，无数据行 |
- **CSV 下载**：列 `row_id, gross_g, tare_g, declared_net_g, expected_rule_ids, fired_rule_ids`，
  UTF-8 带 BOM（Excel 友好），`row_id` 与目标规则编号一致，never 规则不产生行。
- **CSV 回放**：上传 CSV（支持 UTF-8/BOM、GBK，表头支持中英文），按规则重新计算每行实际触发编号；
  含预期列时自动比对并标出一致/不一致，逐行展示供核对。

## 用 Docker Compose 启动

```bash
docker compose up --build -d
# 浏览器打开 http://localhost:8000
docker compose logs -f
docker compose down
```

容器以 gunicorn **单 worker + 多线程**运行（任务保存在进程内存中，请勿调大 worker 数），
内置健康检查；任务仅在内存中保留最近 200 个，重启容器即清空。

## 本地开发（无 Docker 时）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app.main run --port 8000      # 开发服务器
pytest                                     # 41 项测试
python scripts/demo_e2e.py                 # 端到端演示（solo/joint/never 全覆盖）
```

## 目录结构

```
app/
  rules.py     # 规则文本解析、线性表达式与纯 Python 规则引擎（回放复用）
  solver.py    # z3 求解：solo / joint（连带最小化）/ never 判定
  replay.py    # 称重 CSV 生成与回放核对
  main.py      # Flask 路由与内存任务表
  templates/   # 录入页、结果页、回放页、错误页
tests/         # 解析器 / 求解器 / 回放 / Flask 端到端测试
scripts/demo_e2e.py
Dockerfile, docker-compose.yml, requirements.txt
```

## 求解口径说明

1. 先判定规则自身在取值范围内是否可满足（不可满足即 never）。
2. 可满足时，在“触发目标规则”的约束下最小化**同时触发的其他规则条数**：
   最优值为 0 即 solo；否则为 joint，并返回该最优行及其连带规则编号。
3. 回放只信任称重三列与规则文本，`fired_rule_ids` 由规则引擎重新计算，不采信文件内既有结论。
