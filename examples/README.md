# 外部插件生态（examples/）

六个**独立 pip 包**，每个通过 `doc_pipeline.agents` entry_points 提供 Agent：
装好即被本仓零改动地发现 / 注册 / 执行（product-spec §2.1 验收线第 2 条）。

| 包 | Agent | 做什么 | 依赖 |
|---|---|---|---|
| [`plugin-hello/`](plugin-hello/) | `char_stats` | 统计上游正文字符数 / 行数 / 中文字符数 | 无 |
| [`plugin-keyword-extract/`](plugin-keyword-extract/) | `keyword_extract` | 中英混合关键词抽取（词频 + 停用词），供主题覆盖度 | 无 |
| [`plugin-readability/`](plugin-readability/) | `readability` | 可读性维度：平均句长 / 长句占比 | 无 |
| [`plugin-link-check/`](plugin-link-check/) | `link_check` | 链接清单抽取（**不发起请求**，给下游事实核查用） | 无 |
| [`plugin-fact-coverage/`](plugin-fact-coverage/) | `fact_coverage` | 证据密度：数字 / 代码块 / 表格 / 链接占比 | 无 |
| [`plugin-tldr/`](plugin-tldr/) | `tldr` | 离线确定性摘要（标题 + 每节首句，无 LLM） | 无 |

全部是离线、纯标准库、逐字可复现的 Agent —— `link_check` 刻意**不**抓取 URL：
抓取是 fetcher 的活，插件自己去抓就变成网络依赖、不可复现。

## 契约（与内置 `agents/*.py` 同构）

- entry point 的**值必须是模块路径**（`包.模块`），group 固定 `doc_pipeline.agents`；
- 模块顶层声明 `AGENT_NAME`（`AGENT_VERSION` / `AGENT_DESC` / topics 可选），
  并至少定义一个 `BaseAgent` 子类；
- 加载器按 `__file__` 做与内置件同一套 AST 安全检查 —— 不碰黑名单里的调用
  就**不需要**任何声明；`SANDBOX_TRUSTED = True` 只留给随产品发布的内置件；
- 同名冲突时**本仓 `agents/*.py` 优先**，插件顶不掉内置 Agent。

## 试一下

```bash
pip install ./examples/plugin-keyword-extract
python run.py --list-agents      # keyword_extract 带 [entry_point:keyword_extract] 标记
```

整套装：

```bash
for d in examples/plugin-*/; do pip install "$d"; done
```

## 验证（与 CI 同款判据）

- `tests/test_plugin_example.py` —— **示例包**契约：group、模块形状、AST 安全、真 venv 装机即发现；
- `tests/test_plugins_ecosystem.py` —— **生态面**：一次真装六个包，逐个断言
  被发现、被注册、被真实执行、来源标记为 `entry_point:<name>`；数量不足 5 即红。

两条判据的 venv 都用 `--system-site-packages` + `--no-deps`，离线可跑。
