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

- **防御性 catch 是合理的**：`search_engines`（失败 fallback）、`admin_api`（HTTP 500 防护）、`agent.handle`（上报 ERROR 不崩溃）里的 `except Exception` 是刻意设计，**不要机械窄化**
- **禁止静默吞错**：核心写操作（`message_store.save_message`、`checkpoint_manager.save`）让异常自然上浮，调用方决定处理
- **记录后 re-raise**：`base_agent.py:231` 模式（log + raise）是标准做法

### 性能关键路径

- **DNS 解析**：`url_guard.validate_public_http_url` 带 TTL 缓存（正 300s / 负 60s），不要绕过
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

- 阈值：30%（`benchmark.py --ci --threshold 0.30`）
- **失败先看是不是 runner 噪声**：全栈统一慢 30-100% = 环境负载问题（swap/内存），非代码回归
- 刷新基线：`workflow_dispatch` 触发 `refresh-baseline` job

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

**实验性场景**：需求分析（`docreq`）、事实核查（`docgen-verified`）、文档增强（`--enhance`）、MCP Server（`--mcp`）

新增功能请对标主打场景的深度——9 个 Agent、86% 覆盖率、CI 全绿是底线。
