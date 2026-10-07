# 贡献指南

本文档记录项目开发中的关键教训与规范，帮助新贡献者（或未来的你）避免重复踩坑。

## 环境搭建

```bash
# 1. 克隆
git clone git@github.com:Levango7/doc-pipeline.git && cd doc-pipeline

# 2. 创建虚拟环境（Python 3.11+）
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. 安装依赖（requirements.txt 与 pyproject.toml 必须同步）
pip install -r requirements.txt
pip install "pytest>=8.0" "pytest-cov>=5.0" "pytest-asyncio>=0.23" "ruff==0.16.4" "mypy==1.20.2" "types-PyYAML"

# 4. 验证
python -m pytest tests/ -q -m "not e2e"
```

## 测试规范

### 覆盖率门禁

- 当前门禁：`fail_under = 83`（pyproject.toml）
- 源码口径：仅计 `pipeline_core/*`、`agents/*`、`run.py`（排除 `tests/` 虚高）
- 覆盖率跌破门禁即红——**不要为了凑覆盖率写无断言的测试**

### 测试命令

```bash
# 全量（排除 e2e）
python -m pytest tests/ -q -m "not e2e"

# 单模块
python -m pytest tests/test_writer.py -q

# 带覆盖率
python -m pytest tests/ -q -m "not e2e" --cov --cov-report=term-missing
```

### 测试设计原则

- **测行为不测实现**：mock 外部依赖（网络/LLM/文件系统），聚焦模块自身逻辑
- **断言必须真实**：禁止 `assert True`、`assert foo is not None` 等无意义断言
- **平台无关**：测试数据不得依赖本机环境（如 `.env` 密钥、真实 DNS）
- **参数化优先**：`@pytest.mark.parametrize` 替代重复代码

## 代码规范

### 异常处理

- **防御性 catch 是合理的**：`admin_api`（HTTP 500 防护）、`agent.handle`（上报 ERROR 不崩溃）里的 `except Exception` 是刻意设计，**不要机械窄化**。搜索引擎"失败自动切下一个"同属这一族，但实现与判据已随 `search_engines` 迁至 artesian 库
- **禁止静默吞错**：核心写操作（`message_store.save_message`、`checkpoint_manager.save`）让异常自然上浮，调用方决定处理
- **记录后 re-raise**：`base_agent.py:231` 模式（log + raise）是标准做法

### 性能关键路径

- **DNS 解析**：`artesian.url_guard.validate_public_http_url` 带 TTL 缓存（正 300s / 负 60s），不要绕过——实现与判据都在 artesian 库（本仓 fetcher / http_request / event_hook 与库内搜索层共用这一份）
- **SQLite 连接**：`thread-local` 模式，**绝不跨线程 close**（会 SIGSEGV）
- **正则预编译**：热路径正则提为模块级 `re.compile`（fetcher/researcher 已实施）

### 依赖管理

- **pyproject.toml 是唯一事实源**：`requirements.txt` 是其镜像，两者必须同步
- **钉版本**：`ruff==0.16.4`、`mypy==1.20.2`、`pytest==8.3.4` 等已钉死——上游发新版会随机打红 CI
- **新增依赖**：同时更新 `pyproject.toml` 和 `requirements.txt`

## CI/CD

### 工作流

| 文件 | 触发 | 作用 |
|---|---|---|
| `ci.yml` | push / PR | 测试（3.11-3.14 矩阵）+ lint + 安全扫描 + perf 回归 |
| `e2e-nightly.yml` | schedule / dispatch | 真实端到端测试（需 Secrets） |
| `release.yml` | push tag `v*` | 构建 + 发布 GitHub Release |

### 性能回归门禁

`python benchmark.py --ci --threshold 0.30`（CI 默认 `--samples 3`）。判据不是"比基线慢 30%
就红"——那在共享 runner 上就是抽签，历史上它把 `serial 0.02656→0.03671 秒`（差 10 毫秒）
判成 38% 回归，同一个 commit 重跑还能给出相反结论。现在三件事一起成立才判红：

| 判据 | 含义 |
|---|---|
| 环境校正 | 先按**整体环境因子**扣除"整台机器一起慢"：拿各非豁免指标相对基线的"变差倍数"取中位数，且要求参与≥3 项、多数与中位数同向，才用 `(1+原始变化)/env − 1` 校正；达不到就退回不校正（`env=1.0`） |
| 相对阈 | 校正后的变化超过 `--threshold`（默认 30%） |
| 绝对下限 | 变化量超过该指标自己的可测下限（`METRIC_FLOORS`，按本机实测噪声定） |
| 噪声带 | 变化量超过 **2 × 三处噪声估计的最大者**：本轮采样波动、基线记录的波动、最近 20 轮历史跨 run 抖动 |

超阈但被噪声解释掉的项打 **`UNVERIFIED`**：显式打印、退出码 0，不写进基线。
它不是"通过"，是"这台机器此刻测不出答案"——需要时用 `--samples 5` 或
`refresh-baseline` 复核。历史不足 5 轮时噪声带自动缺席，只按前两判。
退出信息里会打印本轮环境因子与"原始变化 → 校正后"，被扣除的幅度是可见的，不是静默放行。

环境校正的代价要写清楚：**一次把所有指标同步拖慢的真实回归会被当成机器慢而放过**。
接受它是因为另一半更糟——共享 runner 上"整台机器慢三成"是常态而"所有函数同时回归三成"
几乎不发生；把后者判成三次回归，门禁就会退化成大家都习惯忽略的红叉。窄域回归（多数指标
稳定、一项慢一倍）仍然照判，`tests/test_benchmark.py::TestEnvironmentFactor` 两条方向相反的用例
钉着这一点，并用 CI run 37360857091 的原始数字（regex +36.4%、selectolax +32.2%、serial +30.9%）
回放了一次。

`并行执行` 的 `process_pool / process_speedup` 属 **`REPORT_ONLY_METRICS`**：
照旧测量、照旧进趋势，但不许判红——它们测的是进程池 spawn 成本，跨次抖 2.6–14.4%（当轮
3 次连续采样实测 91.9% / 51.2%）。`serial / thread_pool` **不在**豁免名单里，回归判定
由 15 毫秒的绝对下限把关（当初那次误报的绝对差只有 10 毫秒）；"并行还能不能用"另由
`tests/test_executor_factory.py` 的正确性断言负责。

- 基线文件同时记录 `_env`（system/machine/python/cpu_count）与 `_spreads`；
  环境指纹变了 ⇒ 相对比较不可信，一律降级为 `UNVERIFIED` 并提示重立基线。
- 刷新基线：`workflow_dispatch` 触发 `refresh-baseline` job（它会同时把历史 JSONL 存进缓存）。
- `benchmark_results.json` 与 `benchmark_history.jsonl` 都在 `.gitignore`；
  CI 用同一把缓存键把它们成对 restore/save —— 只留基线的话波动带永远攒不到样本。

### 发版流程

1. 更新 `CHANGELOG.md`（新增版本段落）
2. 版本号双处同步：`pyproject.toml` + `pipeline_core/__init__.py`
3. 打 tag：`git tag vX.Y.Z && git push origin vX.Y.Z`
4. Release workflow 自动提取 CHANGELOG 段落作为发布说明

## 发版物三步校验

每次发版前必查：

1. `pip install --no-deps -e .` 后 `pip show doc-pipeline` 看 `Requires` 是否覆盖源码全部 import
2. `pip wheel --no-deps .` 后解压 `.whl` 查 `METADATA`（确认依赖/入口/文件清单完整）
3. `pyproject.toml` 与 `requirements.txt` 逐项对照

**CI 全绿 ≠ 发版物可用**——装上能用才是终点。

## 常见踩坑

| 现象 | 原因 | 解决 |
|---|---|---|
| CI 测试全红（4 版本矩阵） | pytest 漂到 9.x + coverage 7.16 | 钉 `pytest==8.3.4` |
| perf CI 间歇性红 | 共享 runner 环境波动 | 看是否全栈统一慢 → 刷新基线 |
| `pip install doc-pipeline` 报 ImportError | pyproject 依赖漏列 | 补齐 6 硬依赖 |
| 测试在 Linux 红、Windows 绿 | 平台相关路径/信号 | 用 `tmp_path` + 跨平台路径 |
| `del sys.modules["run"]` 后 patch 落空 | 模块身份变化 | 用 `importlib.import_module` + `patch.object` |

## 产品定位

**主打场景**：文档生成（`--pipeline docgen`）

**实验性场景**：需求分析（`docreq`）、事实核查（`docgen-verified`）、文档增强（`--enhance`）、MCP Server（`--mcp`）、通用工作流（`api-report`：出网取数 → 声明式变形 → 落盘，不含文档语义）

新增功能请对标主打场景的深度——14 个 Agent、9 条流水线、覆盖率不低于门禁线（`pyproject.toml` 的 `fail_under`）、CI 全绿是底线。
