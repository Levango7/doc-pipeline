# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed（2026-10-07）

- **质量门不再放过"有字的垃圾"（FP-1，产品定义 docs/product-spec.md §6 列的前置必修）**。
  实测现场：本机 keyless 跑 `--pipeline docgen`，落盘 3065 字节、`writer.stats.empty_sections`
  为 4 个（共 5 节正文），正文实为 360 识图页面的导航样板文字——当时 `quality_gate` 给
  **98.8 pass**、`checker` 只报 P3、`done` + `exit 0` 并正常出厂。
  - 根因一：保真底线只有两条判据（非空 + `min_output_chars=120`）与两个占位语，而 writer
    降级时写的是「降级声明」「（暂无可用的相关内容）」，都不在那份名单里 ⇒ 3000 字的垃圾
    照样过长度线。
  - 根因二：`run.py:349` 已经算出 `empty_sections`，却只 print 一条 WARNING，不参与判定。
  - 根因三：评分维度被抓取噪声反向骗过（`substance=100` 因样板文字信息密度高、重复率低；
    `topic_relevance=100` 因页面标题恰含主题词）。
  - 处置：新增 `docpipeline/degradation.py` 作为 writer ↔ 门禁的**单处**契约（占位行、降级
    声明、空结果自述与占比计算），两处不再各写一份字面量；底线补一条结构判据——
    **占位章节占比 > `max_placeholder_section_ratio`（默认 0.34）即 `hard_floor` 失败**。
    阈值取 1/3 而非"命中一次即判死"，是为了守住既有分工：偶尔一节没料属于评分该管的
    "写得好不好"，大半交白卷才是"到底有没有内容"。
  - 现场复验：同一命令现在 `exit 1`，报
    `HARD_FLOOR: 产出未过保真底线: 4/7 个章节是占位符（57% > 34%）`，**且不落盘任何交付物**。
  - 变异验证：把占比判据整块摘掉（`placeholder_ratio → 0.0`）后，同一份文档重新
    `status=pass / score=92.9` 且无 `hard_floor` ⇒ 拦下它的确实是新判据，不是别处顺带生效。
  - 判据：`TestPlaceholderSectionFloor` 五条（实测样本被拒 / 6 节只空 1 节不得判死 /
    阈值可配 / 非法阈值回落默认 / writer 与门禁共用单处定义的源码护栏）。
  - **锁文件影响**：`quality_gate` 的 `CONFIG_SCHEMA` 新增一项 ⇒ 节点有效配置变了 ⇒
    按既有规矩对 9 条含 quality_gate 的流水线重签 `--write-lock`
    （`_quality-tail/docgen/docgen-lean/docgen-render/docgen-verified/docreq/kb-docgen/test_pipeline/three_pass`，
    quality_gate 的 `config_hash` 由 `e3ebdd7e47f8` 变 `db28e2c9363f`）。
    `api-report.lock` 只差 `created_at`/`plan_id`（不参与校验），已还原以免无意义churn。

- **MCP 的 stdio 通道被启动 banner 污染，且 `serverInfo.version` 恒为 `unknown`（FP-3，
  产品定义 docs/product-spec.md §6）**。真子进程跑 `python run.py --mcp` 喂
  `initialize`/`tools/list` 复现：stdout 头四行是框线 banner，之后才是 JSON-RPC 帧——
  严格说这是往协议通道里塞非 JSON 行，`--json-output` 的 wrapper 同样受影响。
  - 处置：`print_banner()` 改走 stderr（stdout 只留机器读的产物通道）。
  - 版本回显的根因是**包初始化次序**：`pipeline_core/__init__.py` 在第 31 行就导入
    `mcp_server`，而 `__version__` 定义在文件末尾（原第 83 行），于是子模块里
    `from . import __version__` 发生在包半初始化时抛 ImportError，被就近的
    `except ImportError: SERVER_VERSION = "unknown"` 吞掉 ⇒ 表面永远报 unknown。
    现在 `__version__` 上移到子模块导入之前，并把那个兜底 except **删掉**——
    取不到版本应当当场炸，而不是伪装成一个能出厂的字符串。
  - 判据：新增 `TestMCPOverRealStdio` 两条（每个请求恰好一帧且 stdout 无 banner 文本；
    `serverInfo.version` 必须等于 `pipeline_core.__version__`）。此前 12 条 MCP 用例
    全在进程内调 `_handle_request`，所以这两个缺陷没有任何测试能发现。
  - 变异验证：banner 改回 stdout ⇒ 两条全红（断言里直接看到框线字符）；
    把 `SERVER_VERSION` 钉成 `"unknown"` ⇒ 版本那条红并打出真实回显字典。均已还原。
  - 真 stdio 用例把 `DOC_PIPELINE_STATE_DIR` 指到 tmp_path：幂等库按检出绝对路径共享，
    跑在仓库 `bus_data/` 上会与真实运行串台。

### Fixed（2026-10-07·续2）

- **CI 三处"绿而未证"收口（FP-2，产品定义 docs/product-spec.md §6）**。这三处的共同形状
  是"判据存在，但判定维度上没人读它的结果"，于是绿灯被当成能力证明。
  - `Pipeline smoke test` 步骤里 `python run.py --check` 跑在 `set +e` 下，**退出码从未
    被读取**，CI 只用 `grep -q "HTML 解析后端: selectolax:"` 卡一行文案 ⇒ 自检里除那一行
    以外的任何结构性故障（缺模块、目录不对、初始化抛异常）都能一路绿。现在把 `rc` 读回
    来并据此红；selectolax 那条 grep 作为**独立**断言保留（它挡的是另一类静默降级）。
    本机 12 条 bootstrap 用例新增 `test_llm_without_credentials_is_warn_not_error` /
    `test_llm_router_crash_stays_error` 锁住这条区分。
  - **缺凭据与装坏了不再混为一谈**：`bootstrap` 的 `LLM 路由器 / 无可用供应商` 由 `error`
    降为 `warn`（路由器真的抛异常仍是 `error`）。否则 keyless 环境永远 `rc=1`，CI 就
    不敢采信退出码——这正是它当初改用 grep 的原因。实测：本机 keyless `--check` 从
    `26 OK / 1 WARN / 1 ERROR, rc=1` 变为 `26 OK / 2 WARN / 0 ERROR, rc=0`。
  - `pip-audit` 进程失败原先被强制 `rc=0`（"跑不起来"当成"跑了且干净"）。现在重试一次，
    仍失败即 `::error::` + `exit 1`。本地对脚本块做三态验证：桩设为失败 ⇒ `rc=1` 并打出
    「查不了」告警；解析出真实漏洞 ⇒ `rc=1`；**干净分支那行 python 解析逻辑与 HEAD 逐字
    一致（去缩进比对为真），本轮未改动**，其在本地桩环境返回 127 只是本机 bash 子进程
    PATH 里没有 `python`（CI 上由 setup-python 提供），不是门禁语义问题。
  - keyless docgen 分支不再报 `SMOKE OK`：改为 `::warning::` + 写入 `$GITHUB_STEP_SUMMARY`
    的 `docgen smoke: SKIPPED (keyless)`。它证明的是"保真底线会拦截"，从来没证明过
    "能成文"，绿灯不该被读成后者。成文能力的证明归属带凭据的 e2e 腿（该腿在同一 SHA
    `14c98b3d` 上目前为 failure，属未收口项）。
  - 语法自证：改动后的两个 `run:` 块 YAML 解析通过（`test` 作业 10 步）且 `bash -n` 无错。
  - **残留风险（须看第一次真实 CI 运行）**：本轮把 `--check` 的 rc 与 pip-audit 的进程
    健康变成硬依赖，若 ubuntu runner 上存在本机没暴露的结构性问题或 PyPI 数据源抖动，
    CI 会**如实变红**——这是修复的目的而非回归，但意味着"CI 全绿"这一条需要重取。

### Fixed（2026-10-07·续3）

- **成品不得夹带抓取层原始素材（FP-1 的余波，由真实 CI 暴露）**。CI run 37504991608 在
  `690c6d7` 上 keyless 产出 **21,926 字节**文档并记为 `OUTPUT FIDELITY OK`；本机同形状产物
  28,412 字节、`quality_gate` 给 **95.5 pass**。占比判据拦不住它——16 个章节里只有 4 个是
  占位符（25% < 34%），其余章节被喂进了 `fetcher._save_article` 的**磁盘格式**：
  `标题: / 来源: / 下载时间: YYYY-MM-DD HH:MM:SS / 60 连等号分隔线`（`agents/fetcher.py:700-703`）。
  - 先试过两个候选指标：段落近重复率、导航样例行占比。实测在真假样本上都不可分离
    （junk 样本 `dup_para_rate=0.167`、`menu_like_rate=0.024`，与真文档同量级）——
    **拿这种指标做门禁等于再造一个假绿**，故弃用。
  - 处置：判据取结构性签名（fetcher 自己的落盘格式），`raw_fetch_block_count() > 0` 即
    `hard_floor`；需要附原文的场景用 `allow_raw_fetch_blocks` 显式放行，且字符串 `"false"`
    不被 `bool()` 判真（`_flag_enabled` 只认显式真值）。
  - 现场复验：本机 keyless docgen 由 `exit 0 + 28.4 KB 落盘` 变为 `exit 1` 并报
    `正文泄漏 3 块抓取层原始素材…`，交付物不再写出。CI 的 keyless 分支因此回到
    `SKIPPED`（诚实），而不是 `OUTPUT FIDELITY OK`（假）。
  - 判据 `TestRawFetchBlockFloor` 五条：素材块头被拒 / 真文档不误伤 / 显式放行生效 /
    `"false"` 字符串不放行 / **签名与 fetcher 落盘格式同源**（用 `inspect.getsource` 断言
    fetcher 格式变了就必须同步改签名，防止判据单方面失效）。
- **researcher 池化从"复制"改为"分片"**。`_build_node_payload` 的分片分支原先要求
  `len(all_queries) >= pool_size`，查询词比池实例少时整体退回全量复制。实测后果：
  `pool_size: 2` 的两个 `researcher_pool_*` 返回逐字节相同的结果，同一条 query 出网两遍。
  （`EXTRACTS_QUERIES` 自 3674de6 起就由 researcher 声明、`AgentLoader` 也会把它搬进
  `AgentMeta` —— 坏的只是这个长度门槛，不是声明缺失。）
  - 处置：条件去掉长度门槛，`all_queries[pool_idx::pool_size]` 天然让多余实例领空活
    （如实回报零结果）。
  - 复验：keyless docgen 实测 `researcher_pool_0 queries=1 results=10` /
    `researcher_pool_1 queries=0 results=0`。
  - 判据 `tests/test_pool_sharding.py` 五条，含"未认领标记的 Agent 仍看全量"（writer 这类
    必须看到完整上下文）与一条**接线证明**（`AgentLoader.register` 后
    `Registry.get_meta("researcher").extracts_queries` 必须为真——模块里写了但没进 meta 等于没写）。
- **`--json-output` 的 stdout 契约**。README 承诺它"输出 JSON 结果供 wrapper 解析"，实测
  stdout 上先有 11 行人类可读过程输出（`[run] 加载流水线配置…`、任务头），wrapper 逐行解析
  必炸。处置：该模式下 `sys.stdout` 整体改接 stderr，结果 JSON 由 `output_json_result`
  显式写回 `_RESULT_OUT`（进入该模式前抓下的真 stdout；不在导入期绑死，否则 capsys 失效）；
  `--mcp` **不做**同样重定向（MCP 的协议帧就走 `sys.stdout`）。
  判据 `tests/test_cli_output_channels.py` 三条（单行 JSON / 过程输出没被弄丢 /
  默认模式的人类报告仍在 stdout，防反向回归）。
- **依赖与文档口径**：`pyproject.toml` 补 `render / ingest / ocr / embed` 四个能力 extras
  （此前 python-docx、reportlab、pymupdf 只在 `requirements.txt` 与 Dockerfile 里，
  `pip install .` 与 `pip install -r requirements.txt` 会装出能力不同的两份东西；
  `paddleocr` 被 `docpipeline/ingest.py` import 却两处都没声明）。下限取自本机实测可用版本：
  `ocr` 必须是 `paddleocr>=3.0` + `paddlex[ocr]>=3.0`（本机 paddleocr 3.7.0 / paddlex 3.7.2；
  2.x 没有 `PPStructureV3` 这个符号，只装 paddleocr 时实例化即抛错——`paddlex` 的
  `Provides-Extra` 里确实有 `ocr`）。`all` 有意不含 `ocr / embed`（它们拉起 PaddleX 与 torch）。
  `docs/deployment.md` 三处硬错更正：`POST /tasks` 提交任务（该路由只有 GET，提交入口是
  `/api/tasks`）、"llm_router 支持 12 家"（定义表 16 家，且只有一条 OpenAI 兼容请求路径）、
  WAL 库"停机后直接拷贝即可"（须先 checkpoint 并带走 `-wal/-shm`）。
  `pipeline_core/llm_router.py` 模块注释从"10 个供应商"改为与定义表同源并写清"16 家是端点与
  定价定义，不是 16 套协议实现"。README 的测试数改为实测值（本机全量
  `2239 passed, 2 skipped, 6 deselected`，截至 2026-10-07）。
- **新增文档一致性护栏** `tests/test_doc_consistency.py`：此前 `tests/` 里没有任何一条测试
  引用 README 或 docs，所以"1854 passed"这类数字落后于代码无人发现。三处口径（6 条用例）
  都从代码取数（`--co` 的收集数、生成的 OpenAPI 路径、`provider_defs` 的 AST 长度），不写死常量。
  变异验证：README 改回 1854 ⇒ 红；文档注入 `POST /tasks` ⇒ 红（两处均已还原）。

### Added（2026-10-06，未发版）

- **子流水线参数化（`call.inputs` + `when.value_from`）**：`call` 节点可以向片段传实参，
  片段节点的 `when` 用 `inputs.*` 读（左值 `path` 与右值 `value_from` 都支持）。
  - `value` / `value_from` 必须恰好给一个；`inputs` 的键不得含点号（点号是路径分隔符）。
  - 作用域三条：普通节点上的 `inputs` 是**默认值**且必须被自己的 `when` 读到；内联时
    只把该节点会读的键落到节点上；嵌套 `call` 各用各的实参，外层不覆盖内层。
  - 解析期双向查参数：引用了没人传 → 报错（`all:` 的短路会让缺参静默绕过）；传了没人读
    → 报错（`min_scor: 70` 这类拼写错误原本会静默走默认值）。
  - `inputs` 参与 `topology_hash`：改实参会让调用方 lockfile 报漂移。
- **出厂片段 `pipelines/_quality-tail.yaml`**：质量尾（quality_gate → checker →
  可选 fact_checker → layout → safe_writer）。四条 docgen 流水线的这一段逐字节比对后
  只差 fact_checker 的有无与门槛，于是差异收敛成 `fact_check` / `min_score` 两个参数：
  docgen 与 docgen-render 传 `fact_check: false`，docgen-verified 传
  `fact_check: true, min_score: 0`（无条件核查），docgen-lean 传 `min_score: 70`
  （达标才核查，取代原先写死在条件里的 70）。
- **片段的目录解析**：`parse_file` 把引用方所在目录传给内联逻辑，于是
  `--pipeline-file /tmp/x.yaml` 能在 `/tmp` 找到它的片段，而不是回头看进程 cwd。
  `_` 前缀的片段不进 `--pipeline` 候选清单（`installed_pipelines` /
  `list_pipelines` / run.py 三处口径一致），但仍可被 `call` 加载。

### Fixed（2026-10-06）

- **内联展开把父图前置接到子流水线的每个节点**：入口判据写成 `d in renamed`，而那时
  `dependencies` 已经是改名**之后**的身份（`renamed` 的键是改名前的名字），于是恒不命中、
  每个内联节点都被当成入口。实测后果：docgen 的 `writer` 成了 fact_checker / layout
  的依赖，层级与并发窗口跟着错，`when` 求值也会因"依赖未成功"被误跳过。
  判据改用改名后的身份集合（`test_parent_edges_move_to_sub_boundary` 现在同时断言
  "非入口不得带父图前置"）。
- **内联节点的 Agent 能力整段失效**：`registry.get_meta(node.agent_name)` 用别名名查表
  得到 `None`，`None` 不抛错，只是能力没了。两处真机撞出来：
  `WRITES_OUTPUT` 失效 → `task.output_path` 不记 → 交付契约反过来说"声明了落盘节点却没有
  交付物"（文件 24 KB 明明在）；`SUPPORTS_REGENERATION` 失效 → 内联的 quality_gate
  不再触发重做，质量门降级成一次性打分。改为按 `agent_of` 后的 base 名查，
  复检目标（`REGENERATION_RECHECK` 缺省取本节点名）也还原成 base 名，否则 RPC topic
  `quality_gate__tail.input` 没有订阅者。判据：`TestInlinedNodesKeepAgentCapabilities`
  两条，把修复回退会双双转红。
- **报表里质量门控警告静默消失**：`run.py` 还用 `(task.result or {}).get("quality_gate")`，
  内联后键名是 `quality_gate__quality_tail` → 拿空 → 低分文档只剩"执行完成"。
  与 writer 一样改走 `_result_for`（同名多处出现时不猜）。
- **`Scheduler.validate()` 查不到内联/池化节点的 Agent 文件**：它用原始节点名拼
  `agents/<name>.py`，`writer_pool_0` 早就拼错（该方法目前无调用方，见待办）。
  改为先 `agent_of()` 还原。（附带记录：`validate()` 全仓零调用方，
  `config_schema.py` 注释里"语法坏掉的 Agent 文件由 validate() 另行报告"因此落空。）
- **YAML 布尔名字陷阱**：`- name: on` 被 YAML 1.1 解析成 `True`，报错落在几千行之外的
  `'bool' object has no attribute 'split'`。解析期加判据并点名这个陷阱。

### Docs（2026-10-06）

- README：`value_from`、`call.inputs` 与默认值/作用域表、`_quality-tail` 片段说明；
  求值上下文表加 `inputs.*` 一行。
- docs/architecture.md：§1.4 补"能力也按 Agent 名解析"两处教训；新增 §1.5
  片段与参数作用域（含锁定覆盖面、`_` 前缀约定、同目录要求）。

### 拓扑与锁文件差异（2026-10-06，四条流水线 + 新片段）

| 流水线 | node_count | topology_hash | 锁里节点名变化 |
|---|---|---|---|
| docgen | 8 → 9 | `6f706b933bce` → `1c3199450b61` | 去 `checker/layout/quality_gate/safe_writer`；加同名 `__quality_tail` 五个（多出 `fact_checker__quality_tail`，`fact_check: false` 时被条件跳过） |
| docgen-render | 9 → 10 | `23d5d9b92290` → `3a28a72ff2e2` | 同上；`renderer` 的依赖由 `safe_writer` 改接到 `safe_writer__quality_tail` |
| docgen-verified | 9 → 9 | `34a01b8c2d1b` → `697f95b94353` | 五个尾巴节点全部换成 `__quality_tail` 身份；`fact_checker` 从"无条件"变成"`fact_check: true, min_score: 0`" |
| docgen-lean | 9 → 9 | `94e9b0597cc8` → `a6b8de38ebd4` | 同上；阈值 70 从写死在 `when.value` 变成调用方 `inputs.min_score` |
| _quality-tail（新） | — → 5 | 新 `f88f21d8a323` | 片段的默认展开：`fact_checker` 带默认值 `{fact_check: true, min_score: 0}`，因此片段自身也能解析/加锁 |

四条都重新 `--write-lock`；存活名字之间 `config_hash` 无一漂移（配置逐字节照抄，只换了归属文件）。
三条 smoke 实跑：docgen `done`/exit 0（fact_checker ⏭️ 条件跳过）、docgen-verified `done`
（fact_checker 实跑）、docgen-render `done`（renderer 因 python-docx 对占位稿报 XML 控制字符
错误而 ❌，与本次抽取无关，另立待办）。

### Added（2026-10-06·续，未发版）

- **docreq / kb-docgen 也接上质量尾片段**（承接上面"四条"的范围，现共六条流水线引用
  `_quality-tail`）。两条的尾巴与片段逐字段相等（threshold 70、max_regenerations 3、
  timeout 300、`fail_fast: false`、`style: markdown`、`backup_dir/atomic`），
  都只要 `inputs: {fact_check: false}`。上一笔写"未引用片段的四条…逐字段未变"，
  那是当时的范围决定而非结论——`three_pass` 才是不该并入的那条
  （threshold 65 / max_regenerations 2 / 超时 120·60 与片段不同）。
- 平价判据（脚本比对，不是断言）：抽取前的 lock（`/tmp/locks_before`）与重锁后的 lock
  逐节点比 `config_hash` / `version` / `pool_size`，六条流水线共 **26 组，0 处不一致**。
- 拓扑与锁文件差异（本笔两条）：

  | 流水线 | node_count | topology_hash | 节点名变化 |
  |---|---|---|---|
  | docreq | 9 → 10 | `06008455dc1f` → `13ab15c3b1b5` | 去 `checker/layout/quality_gate/safe_writer`；加同名 `__quality_tail` 五个（多出 `fact_checker__quality_tail`，被条件跳过） |
  | kb-docgen | 7 → 8 | `e196e27b5b09` → `9297257cbd1d` | 同上 |

### Fixed（2026-10-06·续）

- **perf-regression 门禁改成噪声感知**（`benchmark.py`，#19）。原来的判据是
  "单样本比基线慢 30% 就红"，在共享 runner 上就是抽签：CI run 37350496361 把
  并行执行 `serial 0.02656→0.03671 秒`（绝对差 10 毫秒）判成 38.2% 回归，
  内置的"复验"再判一次还是 38%（两次采样取自同一台被负载污染的机器，复验并不能否证），
  同一个 commit 反复重跑也只是在同一台机器上再抽一次签。现在：
  - CI 默认 `--samples 3`，逐项取**中位数**（一次被抢占的采样拖不偏中位数），
    并算出本轮相对波动 `(max-min)/median`；
  - 判红要同时过三关：相对阈、该指标的绝对下限（`METRIC_FLOORS`，按本机 4 次独立跑
    实测的噪声与量级标定）、变化量超过 2×噪声带（带取本轮波动 / 基线记录波动 /
    最近 20 轮历史跨 run 抖动三者的最大值）；
  - 过不了后两关的项打 **`UNVERIFIED`**：显式打印、退出码 0，绝不冒充"通过"，
    也不冒充"回归"；基线里新增 `_env`（system/machine/python/cpu_count）与 `_spreads`，
    环境指纹变了就整体降级为不可判并提示重立基线；
  - `并行执行` 的 serial/thread_pool/process_pool 划入 `REPORT_ONLY_METRICS`：
    它们测的是池 spawn 成本（本机跨次抖 2.6–14.4%），照旧测量与进趋势但不许判红；
    "并行还能不能用"由 `tests/test_executor_factory.py` 的正确性断言负责。
  - 判据本身有测试：三个方向的变异（取消绝对下限 / 取消只观测豁免 / 噪声退回只看本轮）
    各自让对应用例转红；端到端合成三例——全指标慢一倍 ⇒ exit 1（7 项确认）、
    复刻 CI 那组并行三项 +38% ⇒ exit 0、只让 TF-IDF 真的慢 64% ⇒ exit 1。
  - 附带：`.gitignore` 补 `benchmark_history.jsonl`（此前 untracked 且未忽略，
    `git add .` 会把它带进提交）；CI 的 perf 缓存改为基线与历史**成对** restore/save，
    否则波动带在 CI 里永远攒不到样本；`ci.yml` 里两处 save 步骤同步。
- **上一笔的 Docs 条目领先于文件**：那条说 README 已有"`call.inputs` 与默认值/作用域表、
  `_quality-tail` 片段说明"，实际只有求值上下文表的 `inputs.*` 一行和 `value_from`
  片段两处落地——那次批量编辑里最大的一块（作用域表 + 片段说明）因锚点文本不一致没有
  应用。旧条目保留不删，此处补记。现在 README 的子流水线一节有了 `inputs` 示例、
  作用域表、六条引用关系与 `_` 前缀约定；`pipelines/` 目录树补上 `_quality-tail.yaml`
  与 `docreq.yaml`，并标注 `three_pass` 为何不引用片段。

### Added（2026-10-06·续2，未发版）

- **AgentLoader 复用已导入的模块对象**（#17 收口）：`register()` 原来每次都
  `module_from_spec + exec_module` 并覆写 `sys.modules["agents.<name>"]`，同一个 Agent
  因此存在两份类对象——测试里 `patch("agents.writer.WriterAgent.handle")` 打的是先导入
  的那一份，注册器造的实例用的是另一份，补丁全程空转、用例照样绿（本仓库已因此收回过
  两次"绿了"的结论）。现在默认复用，并区分三种边界：同一文件 → 复用；同名但来自
  另一个目录（夹具、插件目录并存时常见）→ 按本目录文件重新加载；`reload=True` →
  强制换新的热插拔口子。无论复用与否都照旧跑 AST 安全扫描——缓存里那一份可能是普通
  `import` 带进来的，从没走过这道检查。判据：
  `tests/test_agent_loader.py::TestModuleIdentity` 七条（含"注册前打的补丁必须生效"、
  "同名不同目录不得复用"、"reload 真的换对象"、"复用时仍扫描"），
  把复用条件写死成 `False` 会有三条转红。`docs/agents.md` 新增"模块身份"一节。

### Fixed（2026-10-06·续2）

- **perf 门禁的豁免名单按 CI 实测收窄**：e90ebd6 那一版把 serial / thread_pool /
  process_pool 三项一起划成"只观测"，理由写得对不对要拿数据说话——CI run 37357935560
  当轮 3 次连续采样的波动是 process_pool 91.9%、process_speedup 51.2%、elapsed_ms
  38.1%、selectolax 27.1%，而 serial / thread_pool 连前八都没进（本机跨次也只有
  5.3% / 2.6%）。所以豁免只留进程池那两项，serial / thread_pool 回到判定，
  改由 15 毫秒的绝对下限挡住当初那次误报（差 10 毫秒被判 38.2%）。
  新增 `test_thread_and_serial_are_still_gated`：慢一倍必须还判得红，
  豁免不是"并行全家不测"。

### Added（2026-10-06·续3，未发版）

- **通用 Agent 套件（#22）**：`agents/http_request_agent.py` + `agents/transform_agent.py`，
  两个都不带文档语义——引擎"换一种任务类型仍然成立"从此有可运行的例子，而不是一句宣言。
  - `http_request`：出网走 `url_guard.validate_public_http_url`，**没有关闭校验的配置项**；
    要指内网只能显式列 `allow_hosts`，比对 hostname 全等（`db.internal` 不顺带放行
    `evil-db.internal.attacker.com`），命中时产物带 `guard: "allowlist"`。`max_bytes` 超限
    **中止并报错，不截断**（残缺 JSON 会让下游拿到看似合法的坏数据）；3xx 恒不跟随且抢在解析前
    返回 `redirect_to`（重定向体常为空，让"不是合法 JSON"抢先判死就看不见 Location）；
    4xx/5xx 判节点失败而不是把错误页当产物往下传；`expect: json` 解析失败如实报错；
    响应头进产物前脱敏（`authorization`/`token`/`secret`/`cookie`/`api-key` 恒为 `***`）。
  - `transform`：声明式 `items / fields / where / set / template`，求值语言与上下文和 `when`
    同源（`conditions.resolve`），作者不必学第二套取数规则；`len()` 是唯一放行的函数——
    在配置里塞表达式求值器等于把数据通道变成代码通道。缺字段、路径取不到、模板变量取不到
    一律判失败，不渲染成空串出厂。产物 `data` / `text` / `content`（后两个同值，方便直接接
    按 `content` 取正文的 `safe_writer` 与质检节点）。
  - `conditions.resolve` 支持列表下标（`items.0.id`），`when` 与模板同时受益。
- **出厂消费者 `pipelines/api-report.yaml`**：`http_request → transform → safe_writer`，
  三个节点零领域词。它存在的意义是守住接线——能力没进任何流水线就等于零，
  这条已经为 `ingest`/`knowledge_base` 与 `when` 各补过一次出厂消费者。
  判据 `tests/test_generic_agents.py` 41 条：离线跑通真件（requests 换成罐头响应，Scheduler →
  DAGExecutor → 真 transform → 真 safe_writer 落盘），断言的是**交付物本身**——文件存在、
  正文含渲染结果、模板里没有残留的 `{{`；另一条把接口打挂，要求流水线如实 `failed` 并带出
  错误，而不是拿空产物报 done。本机另用真网络实跑过一次（`run.py test_input.md --pipeline
  api-report`，1.4s，三节点全 ✅，落盘产物首行 `# apache/kafka`、正文含真实
  `Stars: 33912` 与 `接口返回码: 200`），离线罐头响应与真实出网两条路都走通。
- 拓扑与锁文件差异（本笔，全新流水线一条）：

  | 流水线 | node_count | topology_hash | 说明 |
  |---|---|---|---|
  | api-report（新） | 3 | 新 `fee618522b1f` | `http_request` / `transform` / `safe_writer` 三层各一节点，无 `call` 内联 |

### Fixed（2026-10-06·续3）

- **perf 门禁加整体环境因子，并修掉入口处的类型混用**（#19 收口）。
  push 出去的 `f235ae3` 在 CI run 37360857091 上被 perf-regression 判红：
  `regex 0.01204 → 0.01642（+36.4%）`，同时 `selectolax +32.2%`、`serial +30.9%` 各自
  因为绝对下限记为 UNVERIFIED。那笔提交只动 `agent_loader` 的模块复用，与这三段互不相干
  的耗时没有因果；三个数同向就是"那台 runner 那天慢"。现在先估**整体环境因子**
  （非豁免指标"变差倍数"的中位数，要求参与≥3 项且多数与中位数同向，否则不校正），
  用 `(1+原始)/env − 1` 校正每项再判三关；被扣除的幅度在消息里明写"原始 X%，已按 env 校正"。
  回放那次 run 的原始数字现在 0 项确认、0 项待证（`test_the_real_ci_red_of_run_37360857091_is_no_longer_red`）。
  - 代价写清楚：**所有指标一起变慢的真实回归会被当成机器慢放过**。换它是因为另一半更糟——
    共享 runner 上整体漂移是常态，而把它判成三次回归等于让大家习惯忽略红叉。
    窄域回归照判（`test_outlier_still_fails_when_the_rest_is_stable`）。
  - **入口 bug**：`main()` 里 `env` 一名两义——先是 `_env_fingerprint()` 的 dict，
    再被赋成 `_effective_env()` 的 float，随后指纹比较那两行对它 `.get()` →
    `AttributeError: 'float' object has no attribute 'get'`，整条 perf job 会当场崩；
    同一处还把 `_env` 写成那个 float，下一轮比较彻底失效。拆成 `fingerprint` / `env`，
    并补 `TestCiEntryPoint` 三条真正走 `main()` 的用例（此前**没有任何测试跑过入口**，
    所以判据测得再细也没挡住）。把 `_env` 改回写 float 会有两条转红。
  - CI 缓存拆成基线与历史**成对** restore/save（`Save perf baseline` 只在成功时写，
    `Save perf history` 恒写），否则历史永远攒不到样本、跨 run 波动带恒缺席。
- **`StructuredLogger` 的后台写线程一次失败即永久静默**（由通用件 E2E 撞出，表现为
  `PytestUnhandledThreadExceptionWarning`）。`log_dir` 是相对路径，每次 flush 按当前 cwd
  解析；进程 chdir 或临时目录被清理后 `open(..., "a")` 抛 FileNotFoundError，而它跑在
  daemon 线程里——线程一死，队列只进不出，之后每一条日志都"入队成功"却永不落盘，
  排查时看到的是"没有异常"。现在 flush 全程包住，目录不在就重建，失败批次计数打到
  stderr 后继续跑。判据两条（`TestWriterThreadSurvival`）分别对应两半：去掉重建 → 第一条红；
  让异常逃出 → 第二条红（修复过程中就实测红过一次，那时 `_get_file()` 还留在 try 外）。
- **通用件第一版只读 `self.config`**：YAML 里的节点 `config` 实际由执行器放进
  `payload["config"]`，于是单元测试全绿、出厂 `api-report` 一跑就报"未给出 url"。
  改为 `{**self.config, **payload["config"]}` 合流，并把这条约定写进 `docs/agents.md` §8。

- **legacy 自动图新增准入位 `LEGACY_AUTO`**：`orch.run()` 那条被冻结的兜底路径按设计
  不读 YAML，它的图 = `registry.deps_order()` = **全体注册件**（`registry.py:219`），
  于是"没有节点级配置就跑不出东西"的件被拉进去必然业务失败——`http_request` 落地后
  `tests/test_resume_recovery.py::TestResumeEndToEnd` 就报了 `未给出 url`（实测双向：
  移走两个新件文件该用例过，移回来 3/3 稳定红；清掉 `checkpoints/` 残留也红，排除脏状态）。
  修法不是把这类件的失败洗成"跳过即成功"，而是让件自己声明不参与 legacy 自动图：
  `LEGACY_AUTO = False`（默认 True，旧件语义不变），`_legacy_agent_order()` 同时供
  `plan()` 预览与 `_run_dag_parallel()` 实跑使用（两处必须同一套准入，否则 `--plan` 里
  看得见、实跑没有）。件照旧注册、照旧被声明式流水线调用。判据三条 + 把过滤器退回
  裸 `deps_order()` 会同时让这条新判据与那条 resume E2E 转红。
  - **顺带记下一处真缺口**（本笔不修，另立待办）：`--resume` 只接在 legacy 分支上
    （`run.py:424`），声明式分支的 `run_plan()` 根本没有 resume 参数；
    `DAGExecutor._merge_resumed_nodes` 依赖 `task._resumed_node_snapshots`
    （`checkpoint_manager.load` 才会设），而只有 `orch.run(resume=True)` 调 `_load_checkpoint`。
    即默认路径下 `--resume` 是空转的，README 的 CLI 表把它写成"从断点续传"因此领先于实现。

### Docs（2026-10-06·续3）

- README：新增"通用 Agent（`http_request` / `transform`）"一节（两张配置表、重定向与脱敏
  语义、`transform` 示例）；目录结构补 `api-report.yaml`，Agent 计数 12 → 14。
- `docs/agents.md`：§8 节点级配置从 `payload["config"]` 来（含 `PRODUCES` / `WRITES_OUTPUT`
  的交付契约提醒）；§9 通用能力必须有出厂消费者。
- CONTRIBUTING：性能门禁一节补"环境校正"判据行与它的代价说明；`REPORT_ONLY_METRICS`
  那段此前仍写着 serial / thread_pool / process_pool 三项——`bead99d` 已按实测收窄到
  进程池两项，此处按代码改正（`benchmark.REPORT_ONLY_METRICS` 实测为
  `['process_pool', 'process_speedup']`）。

### Fixed（2026-10-06·续4）

- **`transform` 里 `where` / `fields` 不给 `items` 时被静默忽略**：两者只在逐项循环里生效，
  没给列表就等于"写了没人读"——作者以为过滤跑了，产物却全量出厂。现在直接判失败
  （`需要 items`），与引擎对 `call.inputs` 的规矩同源。判据一条两个方向：分别注掉
  `where` 或 `fields` 的守卫，用例都会转红（不是只验了第一半）。

- **渲染层拒绝不了抓取正文里的控制字符**（#21，docgen-render 出厂流水线因此 ❌）。
  `renderer` 把正文交给 python-docx，lxml 在写 `<w:t>` 时抛
  `ValueError: All strings must be XML compatible: Unicode or ASCII, no NULL bytes or
  control characters`，整条流水线到此为止。触发样本是抓取来的一页正文里混着的**一个
  `\x08`**（`运行实例 » \x08相关文章`）。pdf 那条走 reportlab 同理。
  现在 `xml_safe()` 在进后端前剔掉 XML 非法控制字符（保留 `\t\n\r`），并把
  **剔了几个**如实放进结果（`control_chars_stripped`），`renderer_agent` 收到非零就
  `log_warning`——这些字符不可见，但不等于"正文被动过"可以静默。
  判据五条（`TestControlCharsAreStripped`）：计数与保留 `\t\n\r`、docx 渲染成功且
  round-trip 后正文完好、pdf 同样成功、干净输入回报 0（不能虚报动过）、脏标题
  （`\x0b`）被清洗后 `core_properties.title` 仍是"季度报告"。
  标题是另一条入口：docx 那边 `core_properties` 写入被 `contextlib.suppress` 包着，
  脏标题**不报错而是静默丢掉文档属性**，所以 `xml_safe` 也作用于 `title` 并计入同一个数。
  把 `xml_safe` 调用换成 `stripped = 0` 会让 docx 与 pdf 两条直接复现上面那句 ValueError；
  单独注掉标题那两行会让脏标题用例转红。
  实跑确认：`python run.py test_input.md --pipeline docgen-render` 现在 10 个节点全 ✅
  （renderer 142.5ms，此前是 ❌），落盘 `output/render_probe2.docx` 47 KB，
  重新读回 docx 后正文里非法控制字符 0 个、Title/Heading 样式照旧在（目录可跳转没退化）。

### Fixed（2026-10-06·续5）

- **未知检索引擎不再回落 mock，docgen smoke 从"间歇抽签"变成确定性**（#25，
  CI run 37373808297 上 docgen smoke 判红的根因）。`docgen.yaml` 配的引擎是
  bocha/tavily/serper，而 `researcher` 里没有这三者的实现分支，原来的兜底
  `else: _mock_search(...)` 把它们静默换成假摘要——实测出过一次 351 字节的
  bocha 占位稿被当成功出厂（同一 commit 重跑又全绿，近 6 次里 1 红 5 绿），
  smoke 门禁（≥800 字节且非占位）因此一直在抽签。
  现在 **mock 必须是显式选择**：未知引擎只告警跳过（`results = []`）⇒ 无可用
  引擎的环境里流水线必然被产出保真底线拦下（`HARD_FLOOR`，rc=1，不落盘），
  ci.yml 的 `rc≠0 + HARD_FLOOR` 分支成为 keyless 路径的固定走向（已加注释）。
  dead-proxy 环境实跑复现：`错误: HARD_FLOOR: 产出未过保真底线: 内容过短（52 < 120
  字符）；产出为占位内容（命中"未采集到可整合的搜索结果"）`，rc=1 且无产物文件。
  判据 `TestUnknownEngineDoesNotFallBackToMock` 三条；另改写两条把 bug 写进断言的
  旧用例（"未知引擎→mock"的期望值、以及从未命中的异常注入）。
- **`--resume` 在声明式路径是空转**（#24，README 的 CLI 表因此领先于实现）。
  `run.py` 默认走 `run_plan()`，而它根本没有 resume 参数——断点状态要
  `checkpoint_manager.load()` 设到 `task._resumed_node_snapshots` 才生效，
  只有 legacy 的 `orch.run(resume=True)` 会调 `_load_checkpoint`。现在：
  - `run_plan(..., resume=False)` / `run_plan_async(..., resume=False)` 接断点，
    run.py 声明式分支透传 `--resume`；`_resume_snapshots_for` 先做**归属校验**：
    断点流水线名与当前流水线不符就拒收并告警——否则节点名对不上时静默全量重跑、
    对得上时用错结果，两种都不是"续传"。
  - `DAGExecutor._merge_resumed_nodes` 改成**按层增量**合并：`_execute_plan` 每层
    现场新建 TaskNode，原先"每任务一次"的合并只能覆盖第一层，后面几层的已完成
    状态永远合不进来，resume 退化成"除第一层全部重跑"；已合并过的节点记录在案，
    节点重跑出新结果后不会被旧快照倒回去。
  - 顺带对齐一处 async 差异：`run_plan_async` 此前无条件盖 DONE，声明了落盘节点
    却没有交付物也报 done（SSE 路径），改为与同步版同一交付契约。
  - 判据四条（`TestDeclarativeResumeEndToEnd`）：声明式 E2E（4 个已完成节点复用、
    下游 3 节点真跑、交付物落盘且含恢复内容）、async E2E、归属拒收、async 交付
    契约。两处变异实测转红：摘掉快照挂载 ⇒ 4 个已完成节点全部重发 bus.request；
    注掉交付契约 ⇒ 无产物仍报 DONE。
- **Docs**：README 的 `--resume` 行按实现改写（两条路径都生效，流水线归属不符会被拒收）。

### Added（2026-10-05，未发版）

- **Phase 1 解耦——产物契约**（`pipeline_core/artifacts.py` + `config_schema.py`）：
  节点之间传什么由 Agent 自己声明，引擎不再持有领域词表。
  - Agent 声明 `PRODUCES`（`{"content": "last"}`）/ `CONSUMES`，Scheduler 与
    DAGExecutor 按声明组装载荷；"谁的内容更新"由 DAG 层级决定，
    取代写死的 `content_priority = ["layout","quality_gate","writer","fact_checker"]`
    与 `articles`/`results`/`spec` 字面量拆解。
  - 配置契约从 `scheduler.AGENT_SCHEMAS`（9 个内置 Agent 的集中表）下沉为各
    Agent 模块的 `CONFIG_SCHEMA`，Scheduler 用 **AST** 读取（不执行 Agent 代码）；
    默认值注入改为深拷贝，避免同池实例共享同一个 list 对象。
  - 沙箱信任由 Agent 自证（模块顶层 `SANDBOX_TRUSTED = True`，加载器 AST 核实），
    删除 core 里 13 个名字的 `_TRUSTED_AGENTS` 名单（含 `fast_pool_0` 测试遗留项）。
  - 重做循环不再猜目标：声明 `SUPPORTS_REGENERATION` 必须给 `REGENERATION_TARGET`；
    反馈载荷不再拆 `quality_scores`/`citation_report` 等 gate 专属键（实测无 Agent 读），
    整体转发为 `gate_feedback`。
  - **P1-4 接口层去领域化**：Admin API / MCP / OpenAPI 此前各自把 `"docgen"` 写成
    默认流水线名，并各写一份"从 result 里翻 safe_writer/layout/checker 找产出"的
    猜测。现在：落盘节点声明 `WRITES_OUTPUT`，引擎把交付物挂到
    `task.output_path/output_content`，接口层共用 `artifacts.task_output`；
    默认值改由 `config.default_pipeline` 提供（纯函数
    `scheduler.resolve_pipeline_name` 解析，歧义时报错列出可用清单而不猜）。
    OpenAPI 的 `pipeline` 字段改为 enum 列举真实流水线。
- 护栏：`tests/test_artifact_contract.py` 的 `TestCoreStaysDomainNeutral` 用 AST
    扫 11 个 core 模块的代码级字符串常量，领域 Agent 名一旦回流 core 即失败。

- **kb-docgen 流水线**：把此前未被任何流水线引用的摄入层 / 向量知识库接进编排，
  实现"本地资料 → 建库检索 → 知识库接地写作"。离线（无 LLM Key）也能交付
  抽取式草稿，`tests/test_kb_pipeline_wiring.py` 覆盖 21 例（含整条 DAG 离线跑通）。
- **产出保真底线**（`agents/quality_gate.py`）：内容长度与已知占位语两条判据，
  命中即 `hard_floor` 失败，不重做、不因 `fail_fast=false` 放行，流水线 exit 1。
- **`pipeline_core/selectolax_compat.py`**：HTML 解析内核兼容层（modest/lexbor），
  按真实可导入性选内核，降级计入 `_parser_fallbacks` 并首次告警。
- **`pipeline_core/state_paths.py`**：运行态路径可重定向
  （`DOC_PIPELINE_STATE_DIR` / `DOC_PIPELINE_VERSIONS_DIR`）。
- **edges 一致性校验**（`scheduler.py`）：`topology.edges` 与 agent.dependencies
  双向比对；七条流水线全部补齐 lockfile（此前只有 2 条受漂移护栏保护）。
- **分层拆包：文档领域层独立成 `docpipeline/`**（Phase 1 收口）。
  `renderer.py` / `ingest.py` / `document_enhancer.py` 从 `pipeline_core/` 移出，
  `pipeline_core` 不再导出 `DocumentEnhancer`。这三个模块此前不依赖任何领域无关
  引擎能力（`ingest`/`renderer` 根本不 import `pipeline_core`，只在函数内惰性加载
  可选后端 docx/reportlab/pymupdf/paddleocr/mineru；`document_enhancer` 只用到
  `llm_router` + `search_engines`），是 engine 里仅剩的文档域代码。
  依赖方向钉成单向 `agents → docpipeline → pipeline_core`，由
  `tests/test_layering.py` 用 AST 扫描当门禁（含函数体内的惰性 import 与
  `importlib.import_module("…")` 字面量）。四条判据都做过注入式正例验证：
  往 core 塞一句 `from docpipeline import renderer`、往 ingest 塞 `import numpy`、
  把 `python-docx` 从 requirements 注释掉、往 renderer 加 `import scripts…`——
  各自转红，确认"0 违规"是结论而不是空转。
  docpipeline 的非标准库依赖改为显式登记制（`pipeline_core` / `scripts` /
  已声明的 docx·reportlab·pymupdf / 有意不进 requirements 的 paddleocr·mineru），
  新增外部依赖必须先在登记表认领；其中 `docpipeline → scripts`
  是钉住的历史耦合，只允许 `document_enhancer` 一处。
  `docpipeline/__init__.py` 不做顶层 re-export，避免同一函数出现两个打桩入口。
  CI 的 py_compile/mypy/bandit 口径与 pyproject `packages`/coverage `include`
  同步纳入 `docpipeline/`，否则搬出去的代码会静默脱离门禁。

- **阶段 2-2：常驻 worker 补上队列的消费端**（`pipeline_core/worker.py` + `--worker`）。
  此前 `TaskQueue.acquire()` 没有任何调用方（全仓 grep 只命中定义与 docstring 示例），
  `POST /api/tasks` 入队之后必须有人再手动跑一次 `run.py --recover` 才会真的执行——
  队列只进不出，"提交即排队"在跨进程部署里并不成立。
  - `TaskWorker`：`recover(stale_seconds)` 回收崩溃租约（要求 owner_pid 已死且 started_at
    过期，活进程正在跑的任务不动）→ `acquire(worker_id)` 原子 claim → 按名解析 YAML
    （锁文件漂移直接拒绝执行并写 failed，不退回 legacy）→ `run_plan` → 落终态。
    `run_forever` 支持 stop_event 与 `--idle-timeout`，SIGINT 处理完当前任务才退出。
  - `TaskQueue.finish()`：**终态只写一次**。`update_status` 是无条件覆写，而 worker 化之后
    API 取消（行已 cancelled）与流水线收尾会并发，取消会被洗成 done；两条收尾路径
    （`_finalize_plan_task` / `_finalize_task_queue`）都改用它。
  - CLI：`--worker` / `--once` / `--poll-interval` / `--idle-timeout` / `--lease-stale`。
  - 测试 `tests/test_worker.py` 15 例，两个关键判据都做过反向验证：
    去掉 acquire 的 `AND status='pending'` → 互斥用例转红；
    把 `finish` 改回无条件覆写 → 6 例转红。第一版互斥用例用同一个 `TaskQueue`
    实例开两个线程，被实例内的 `threading.Lock` 先串行化了，SQL 守卫根本没被行使，
    反向验证时**没有转红**——那是假绿，已改为两个独立句柄（等价于两个进程）。
  - 本机实测跑通真实队列：worker claim 到两条历史 kb-docgen 任务，因 input 文件已不存在
    而如实写 failed（错误串同时带"文件不存在"和交付契约原因），不是静默跳过。
  - bandit 拦下我自己写的 B608：`finish` 里按 `allow_states` 长度拼 `IN (?,?,…)` 被判
    "string-based query construction"（MEDIUM，`-ll` 门禁会红）。改成先读当前状态、
    再用常量 SQL 把观测值放进 WHERE——观测值参与条件同样挡住"读到 cancelled 之后
    状态又变"的竞态（状态一变 rowcount 归 0）。改后 `-ll` 零命中。
    另注：本机 `bandit` 控制台脚本是坏壳（`--version` 无输出、退出 1），要用
    `python -m bandit`；CI 上脚本正常。

- **阶段 2-3 第一步（地基）：节点身份的解析收敛成一处**（`pipeline_core/naming.py`）。
  "从节点名还原执行它的 Agent"此前以 `split("_pool_")[0]` 的形式重复在 12 处
  （dag_executor 8 / pipeline 2 / scheduler 2），语义还略有差别。要做子流水线内联
  （同一 Agent 在一棵图里出现多次，`writer__review`）与 foreach，必须先有唯一解析处，
  否则加一种命名形态就要改 12 个文件。约定 `agent[_pool_i][__alias]` +
  `agent_of / pool_index_of / alias_of / node_id`。
  行为零变化（现名里没有 `__`，`agent_of` 对它们恒等）：全量 2013 passed 不变；
  新增 `tests/test_naming.py` 23 例，其中一例遍历 `agents/` 的 `AGENT_NAME`，
  保证没有真实 Agent 名会被这层解析削掉一段。

- **阶段 2-3：子流水线 `call`（解析期内联展开）**。一条流水线此前只能是一张扁平
  DAG，没有"把另一段流程当节点"的能力。
  - 做法是**计划层展开**而非新增执行语义：`_expand_calls` 把被引流水线的节点带别名
    内联进调用方图（`checker` → `checker__review`），call 节点的前置接到子图入口、
    依赖它的节点改接子图出口，然后按依赖重算层级。于是幂等键、检查点、重试、熔断、
    `when`、产物契约全部照旧，不需要在引擎里再维护一套子流程语义。
  - 同一 Agent 因此可以在一张图里出现多次而互不覆盖（父子都写 checker →
    `checker` 与 `checker__review`）。
  - 解析期拒绝：环引用（按**被引流水线名**判，两条节点复用同一子流程是合法复用）、
    嵌套超过 3 层、被引流水线不存在（报可用清单）、call 节点自带
    config/when/pool_size/rate_limit（配置属于子流水线，两处真相不可读）。
  - 拓扑指纹按**展开后**的图计算：只改子流水线一个字节，调用方 lockfile 就报漂移
    （含"节点数不变、只换 Agent"这种靠 node_count 抓不住的情形）。
  - **揪出我自己刚引入的回归**：命名收敛那一步把闭包分组、池归并、结果读回退
    一并改成了 `agent_of`，别名被剥掉 → `layout__review` 掉出上游闭包、
    两段内联池结果互相串台。补 `family_of()`（保留别名、只去池下标）并把三类用法
    分开；`TestRuntimeIdentity`（链式 call 拿到的是上一跳出口内容）与
    `TestPoolMergeIsAliasAware` 各做过变异回退验证会转红。
  - 测试：`tests/test_subpipeline.py` 19 例（展开/边界接线/层级重算/复用不撞名/
    池化下标保留/环·深度·配置拒绝/锁漂移/运行期身份隔离）+
    `tests/test_naming.py` 增至 31 例。README「子流水线」与 architecture §1.4 记录约定。

- **阶段 2-1：条件节点 `when`——拓扑第一次能表达"看结果决定"**。
  此前 `topology.levels` 是手写静态层级，全仓 `condition/branch/loop/foreach/sub_pipeline`
  零命中，DAG 一旦确定就照跑。新增 `pipeline_core/conditions.py`（受限声明式，
  纯函数、不 exec/eval）+ Scheduler/`ExecutionNode`/`AgentConfig` 接线 +
  `TaskNode.skip_reason`。
  - 语义：条件不成立 → 节点标 `skipped` + `skip_reason="condition"`、不投递消息，
    且**下游照常执行**（可选分支不该让整个下游停摆）；因依赖失败被级联跳过的依赖
    仍然让下游跳过，两条路径靠 `skip_reason` 区分，各有用例钉住。
  - 求值上下文：`artifacts.*`（上游按 PRODUCES 合并的产物）/ `upstream.<node>.*`
    （原始结果）/ `config.*` / `pipeline` / `task.*`。
  - 三条硬规矩：算子白名单与写法在**解析期**校验（`ValueError: [agent] when 条件非法`）；
    `bool` 不与数字比较（`True == 1` 在 Python 里会静默成立）；
    **路径取不到直接抛 `ConditionError`，整条 run 失败**——当成"条件不成立"就是
    静默跳过分支却照报 done，正是本项目一路在关的那类洞。
  - `when` 进 `topology_hash`：加条件会让既有 lockfile 报拓扑漂移；不写 `when`
    的节点不贡献条目，内置流水线指纹实测未变、锁文件全部校验通过
    （`test_shipped_pipelines_keep_their_hashes` 用 glob 逐条 `verify_lock=True`，
    新增流水线不会被这条退出门漏掉）。
  - **出厂消费者 `pipelines/docgen-lean.yaml`**：语言实现完就用它接了一条真实流水线
    ——fact_checker 挂 `when: upstream.quality_gate.overall_score >= 70`，
    质量分不达标的草稿不再付核查成本（迟早要重做），达标才升级核查。
    需要无条件核查仍用 `docgen-verified`，这条的核查是可选的，YAML 头部与 README
    场景表都写明了这个区别。
    接这条流水线当场抓出我自己的设计缺口：`_condition_context` 原先只暴露**直接依赖**，
    而 `fact_checker` 的直接依赖是 `checker`、`quality_gate` 是祖先节点，于是 shipped
    配置一跑就抛"路径取不到"。已改为纳入整个上游闭包（`_upstream_closure`）——
    作者按直觉写的条件不该因为依赖图的跳数而失效。这条测试是
    `TestShippedConsumer` 三例：低分跳过且 layout 照跑、高分执行、指纹未变。
  - 交付契约与之衔接：落盘节点全部被条件跳过 → 视为"按声明本轮无交付"，run 仍 done
    并留 warning；因依赖失败而没跑 → 仍然 failed（#13 关掉的洞不借此复活）。
  - 测试：`tests/test_conditions.py` 56 例（语言本身，含"每个算子都可用"的对照表）+
    `tests/test_condition_nodes.py` 16 例（接线行为）。三条判据各做过反向变异并确认转红：
    移除"条件跳过不阻塞下游"的例外 → 1 例红；把求值错误降级成"条件不成立" → 1 例红；
    `when` 不进指纹 → 2 例红。还原后全绿。

### Fixed（2026-10-05）

- **SSE 流式回调从来没挂上过**：`admin_api._find_streaming_agent` 遍历
  `registry._agents.values()`——那存的是 `AgentMeta.to_dict()` 的 dict，
  `hasattr(dict, "handle_streaming")` 恒为 False，于是方法恒返回 None，
  API 提交的任务静默失去增量内容。改为按能力在实例上查找。
- **`embedder: auto` 冷导入 sentence_transformers 78 秒**（拉起 torch）：
  auto 只需知道"有没有这个候选"，改用 `find_spec`；真要用（模型已缓存）
  才付加载成本。kb-docgen CLI 实测 78s → 3.9s。
- **默认流水线跑错定义文件**：`--pipeline docgen` 因 `glob("docgen*.yaml")` 前缀
  匹配 + 首个成功即返回，实际一直在跑 `docgen-render.yaml`；改为精确同名优先、
  多义显式报错（`run.py`）。
- **正文提取静默降级**：selectolax 1.0 起 `selectolax.parser` 在导入期主动报错，
  生产安装（CI 实测 selectolax-1.0.0）恒久退化为正则启发式，而 `--check` 只探测
  顶层包仍报 OK；现在走兼容层并让 `--check`/CI 冒烟如实报告所用内核。
- **四类"静默绿"**：幂等键命中历史返回 None 被记成 success；重试结果的业务失败
  未置 `retry_ok=False`；重做循环末尾无条件把 `status=error` 洗成 `pass`；
  checker 把"检出 P1"表达成 `status=fail` 从而跳过下游。
- **KnowledgeBaseAgent 未订阅 `knowledge_base.input`**：DAG 用
  `f"{agent}.input"` 做 RPC topic，缺订阅时请求静默空转；新增总线寻址护栏。
- **节点 config 不生效**：知识库的 `embedder/db_path/top_k` 与 checker 的
  `block_on_p1` 现在在 handle 期应用（Agent 经 registry 加载时构造期 config 为空）。
- **`embedder: auto` 触发模型下载**：huggingface.co 不可达时按 1/2/4/8/16s 退避
  重试 5 次，CLI 跑 kb-docgen 表现为挂死（实测 >240s 未结束）。
  第一版只设 `HF_HUB_OFFLINE` 不够——sentence-transformers 仍会去查 Hub 的
  revision；现在 auto 先用 `model_is_cached()` 判本地缓存，没缓存**根本不构造**，
  有缓存才套离线环境。修完 kb-docgen CLI 实测 28.8s 跑完并产出接地文档。
- **ingest 初始化实例化 OCR 后端**：为一条日志付 84 秒（本机实测），改为按需探测。
- **运行态污染**：测试与真实运行共用 `<checkout>/bus_data`（幂等记录积 1116 条）
  与 `versions/`（260+ 死条目，使 `/api/versions/stats` 慢到 4.9s）；测试状态
  隔离到 `.test_state/`。
- **E2E Nightly 假绿**：未配 Secret 时 6 skipped / 1674 deselected 仍报 success；
  现在无 Secret 或实际执行 0 用例一律失败。
- **docgen 默认流水线拿不到任何检索结果**（推送后 CI 才暴露，本机同样复现）：
  接线 ingest 进流水线时给它多挂了一个 `researcher.input` 订阅，于是发给
  researcher 的定向 RPC 被 ingest 接走，回了 `{"status":"error","message":"未指定待摄入文件"}`。
  两层判据同时失守把它洗成了"成功"：`_business_failure` 只认 `blocked/fail` 与
  `"error"` 键（这份回执两者都不满足），契约层则原样把空产物铺给下游——
  researcher 记为 success、fetcher 收到空 results、writer 产出 52 字符占位文。
  修复：ingest 只订阅自己的主题；`status: "error"` 归入业务失败状态
  （`_BUSINESS_FAILURE_STATUSES`，与 `_record_task_output` 用同一套语义）；
  业务失败异常带上节点名（原来只有裸 message，排查时认不出是谁报的）。
  新增两条护栏——`TestBusAddressing` 禁止任何 Agent 订阅别人的 `<agent>.input`，
  池化上游声明的产物必须出现在下游 payload 顶层（此前只测了合并这一步）。
  修复后同一条命令实测：`状态: done`，产出 26473 字节（修复前 52 字节 exit 0）。
- **error 判为失败之后揪出的两处真缺陷**：
  - `knowledge_base` 的 `handle` 推断出 `action=search` 后一律走单条 `query` 的
    `_do_search`，而 DAG 节点载荷给的是 `queries` 列表——带查询词的检索节点
    其实一直在回"未指定查询词"。改为有 `queries` 就走 `_do_search_multi`，
    并补 `test_inferred_search_uses_queries_not_only_single_query`。
  - 引入 `status: "skipped"` 表达"无事可做"：ingest 没有语料、kb 没有查询词，
    既不该判失败（legacy `run()` 会给每个已注册 Agent 都发一次 RPC），
    更不该记成交付——`_apply_node_success` 现在把这类节点步骤写成 `skipped`。
    显式 `kb.search` 请求缺 `query` 仍然算 error。

- **`fail_fast: false` 把"没交付"洗成 done**：`_execute_plan` 收尾只要状态不是
  FAILED/CANCELLED 就无条件盖 DONE，于是末端节点保持 RUNNING（软失败不中断）也能
  "完成"。实测 kb-docgen：ingest 如实 skipped、knowledge_base 失败、writer 之后从未执行，
  结果报 `状态: done` + exit 0，而 `--output` 指定的文件根本不存在。
  现在加了交付契约：计划里声明过 `WRITES_OUTPUT` 的节点存在时，必须真拿出交付物
  （`output_path` 指向的文件存在，或有非空内联内容）才算 done，否则 FAILED 并把原因
  并进 `task.error`。实测：kb-docgen 空语料改为 `状态: failed` + exit 1（错误里同时
  给出"节点失败原因；声明了落盘节点却没有交付物"），docgen 正常交付不受影响
  （done / exit 0 / 20471 字节）。
- **mock E2E 的补丁是空转的**（`tests/test_e2e_mock.py`）：`agent_loader` 以
  `spec_from_file_location` 重新加载 `agents/*.py` 并覆写 `sys.modules["agents.<name>"]`，
  因此在 `register_agents()` 之前进入的 `patch("agents.writer.WriterAgent.handle", ...)`
  绑到旧模块对象，对真正实例化的类无效。探针实测：`实例的 handle is mock: False`。
  结果这个"Mock 端到端测试"跑的是真 writer/真 quality_gate，只靠
  `assert "writer" in task.result` 这种弱断言通过。现改为注册之后再对
  `sys.modules["agents.writer"].WriterAgent` 打补丁，并**断言 mock 的哨兵字符串
  真的出现在产出里**——补丁失效时测试自己会红。
  loader 覆写 `sys.modules` 造成同名模块两份对象（模块级锁/缓存各一套）这个设计问题
  仍未处理，已另立待办。

### Docs（2026-10-05）

- README 数据校准：测试数（本机 1854 passed / 2 skipped，CI 1772 passed）、
  Agent 与模块计数、场景表补 kb-docgen、CLI 参数补 `--write-lock` / `--check`、
  示例 YAML 的 `safewriter` 拼错修正，并新增保真底线一节说明其与评分维度的分工。

## [3.9.1] - 2026-08-30

### Fixed

- **发版物 pyproject 依赖不完整**：`[project] dependencies` 此前只列 3 个包
  (PyYAML/requests/selectolax)，但源码 import 还硬依赖 `aiohttp`、
  `orjson`、`duckduckgo-search`——新用户 `pip install doc-pipeline`
  会立即 ImportError。补齐 6 个硬依赖（与 requirements.txt 对齐），
  wheel 重新构建后全栈 import 走通。

## [3.9.0] - 2026-08-30

### Test（覆盖率大补 + 测试可靠性）

- 覆盖率 65.59% → 85.97%（门禁 fail_under 63 → 83）：新增 9 个测试文件 +448 用例，
  补齐 writer/researcher/search_engines/admin_api/fetcher/layout/checker/
  event_hook/cache_manager/run 十个薄弱模块（writer 32→96%、checker 38→100% 等）
- 修复 fetcher 测试依赖真实外网 DNS 的 CI 脆弱点（11 用例改确定性 DNS 映射）；
  修复 duckduckgo"未安装分支"依赖环境假设；修复 2 个 Windows 路径样本在
  Linux 的假通过/假失败；test_run_ext 消除 sys.modules 模块身份陷阱
- 全量 1426 passed / 2 skipped，新增版本一致性护栏（pyproject ==
  pipeline_core.__version__，防发版漂移）

### Performance（实测闭环验证）

- 冒烟实测端到端 researcher **122.6s → 35.6s（-71%）**：
  - duckduckgo 不可达网络（DNS 污染）降序至 HTML 引擎后 + 60s 快速失败窗口
    （原每次查询固定烧 30s 双栈超时）
  - search_with_sites 双护栏：常规满额跳过站点批次；站点搜索只用首引擎
    （9 站点 × 多引擎串行超时 → 单引擎）
- url_guard DNS 解析 TTL 缓存 + 负缓存（20 页下载最坏 300 次同步 DNS → ≤20 次）
  + 字面 IP/DNS 判定 lru_cache
- SSE 推送事件驱动唤醒替代 5Hz 轮询（推送延迟 200ms → 亚毫秒，空闲唤醒 -80%）
- /api/logs 流式读取 + mtime 早退（内存 O(文件) → O(1)）
- fetcher/researcher 热路径正则模块级预编译（正文提取 1.54x，等价性逐字节验证）
- cache_manager file 后端 size() 版本+TTL 缓存；run.py 流水线名进程内缓存

### Fixed

- **SQLite 跨线程 close 段错误（CI Linux 稳定复现，Windows 不可见）**：
  message_store/task_queue 的 close_all 跨线程关闭连接与投递线程执行中
  语句在 C 层竞争触发 SIGSEGV；改为失效标记 + 拥有线程自愈重建
- researcher._normalize_results 运算符优先级 bug（dict 输入恒返回空列表）
- 版本漂移：pipeline_core.__version__ 3.7.0 与 pyproject 3.8.0 不一致
  （banner/API/MCP 对外全报错版本）
- run.py 无 Agent 加载时不再漏 shutdown；事件钩子 webhook 逐跳 SSRF 复检

### CI / 基建

- 测试工具链钉版（pytest==8.3.4/pytest-cov==7.1.0/coverage==7.15.2/
  pytest-asyncio==0.25.2）——浮动安装漂到 pytest 9 后 Linux 全矩阵红
- 修 perf 基线三重污染：save 失败 run 不再污染基线缓存（always()→success()）；
  清理 10 条污染缓存；新增 workflow_dispatch 基线刷新 job
- e2e-nightly pytest 同步钉版

### Removed

- 清理 agent 工具残留目录（.zcode/、.inscode/ 项目快照副本）

## [3.8.0] - 2026-08-26

### Security（深度审计修复 — 3路并行审计，~45项发现）

- **P0 SSRF 全裸修复**：fetcher sync/async 出网请求接入新增 `pipeline_core/url_guard.py`
  （私网/环回/云元数据/DNS解析逐记录校验），redirect 改手动循环逐跳校验（上限5跳）；
  公网302跳内网/169.254元数据端点路径封死（58项新测试）
- **P0 断点续传静默数据损坏**：checkpoint load 恢复 DAG 节点状态，execute_level 跳过已完成节点
  不再重发 bus.request；resume 后 attempts==0 节点绕过持久化幂等键；save 改原子写且失败上浮
- **P0 成本控制双失灵**：`check_budget()` 接线进 llm_router chat/chat_async 调用前（超预算抛
  BudgetExceededError）；chat_async 补记成本（writer 主力路径不再漏记）；PRICING 补
  openai/deepseek/moonshot/qwen/default；响应 usage 字段优先计费
- **P1 error字典被判成功**：`{"error":...}` 结果进入业务失败通道（此前下游拿空数据绿色DONE）
- **P1 message_bus 持锁投递地雷拆除**：REQUEST/RESPONSE 移到锁外 deliver；订阅者异常立即回覆
  错误响应（消灭单节点空烧 node.timeout×max_retries≈20分钟）
- **P1 scheduler 同层依赖校验漏洞**：同层依赖现在正确报错（此前并行执行确定性读到空上游结果）
- **P1 熔断器/限流器切 time.monotonic()**：NTP 回拨不再把令牌桶扣成负值持续拒流
- **P1 进程池 BrokenProcessPool 自愈**：销毁中毒单例重建并重试一次，子进程崩溃不再永久毒化 auto 模式
- **P1 CORS 敞口收口**：Origin 白名单（同源/回环/ADMIN_CORS_ORIGINS env），移除通配 ACAO:*
- **P1 pipeline.started 事件配置脱敏**：api_key/token/secret/password 叶子值 ***redacted***
  （此前明文落盘消息库并广播 webhook）
- **P1 CLI 契约**：--pipeline 拼错报错列出可用名退出码2（此前静默改跑第一个yaml）；
  任务 FAILED 进程退出码 1（此前恒 exit 0）
- **P1 config 热更新半写容错** / **version_manager 索引原子写+损坏安全模式（不清零历史）** /
  rollback 写回前自动保存当前内容 / quality_gate 英文功能词不再误判专名（消灭无谓三轮重烧） /
  scripts/safe_writer 对齐 os.replace 原子替换

### Fixed

- `/stream/metrics` 恒为零的功能性失效（改为跨任务聚合快照）
- SSE 统一 StreamEvent 序列化（ts/section/total 上线）+ 15s 心跳帧 + 客户端断连取消流水线止损
- MCP 业务失败改 isError:true 语义；get_task 校验 task_id；generate_document 支持 output；
  流水线目录锚点统一项目根；mcp_ 临时输入文件清理
- Admin API 错误格式统一（503/400 语义、兜底不泄内部异常）；task_{id}.md 输入临时文件清理
- openapi_spec：TaskInfo 枚举补 paused、/stream/metrics 内容类型、补 / 与 Last-Event-ID 声明
- streaming pause 自旋空转（Event→Condition）；Registry get_or_create 支持配置热更新
- dashboard：API_BASE 同源化、progress 百分比换算修正

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added

- **dashboard 新建任务卡片**：query/pipeline(下拉含 docreq)/output 表单提交 POST /api/tasks，
  running 任务展开 EventSource 实时章节进度——docreq/docgen-verified 获得 UI 触达
- `pipeline_core/url_guard.py` 共享 URL 安全校验模块
- 新增测试 ~200 项（642→840 passed），ruff/mypy 零告警
## [3.7.0] - 2026-08-25

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（需求分析器）
- **requirements_analyzer 需求分析 Agent**：流水线最前端的意图解析节点，把用户输入
  解析为结构化 `DocumentSpec`（doc_type / scope / audience / depth / constraints /
  sources / template / language），供下游 researcher 与 writer 消费：
  - 双路径：有 LLM 走一次小调用生成 JSON（含枚举校验与置信度钳制，非法值回落默认），
    无 LLM / 失败时回退规则引擎（类型·深度·读者提示词匹配 + 关键词提取 + URL/文件引用收集）
  - 歧义检测：输入过短、类型不明、受众未指定时降低 confidence 并生成追问建议
    （field/question/suggestion），confidence 低于阈值（默认 0.7，可配）标记
    needs_clarification，追问条数上限可配（max_questions）
  - 下游接线：dag_executor 将 spec 注入所有后续节点 payload；researcher 用 spec.scope
    补充检索词；writer 用 doc_type 前缀标题、scope 兜底主题、audience 记录读者水平
- 新增 `pipelines/docreq.yaml`（9 层 DAG，首层 requirements_analyzer）。
  命名避开 `docgen*` 前缀以保持既有默认流水线解析顺序不变；
  运行方式：`python run.py input.md --pipeline docreq`

### 测试
- 新增 tests/test_requirements_analyzer.py（24 项）：规则分析各维度、DocumentSpec
  往返序列化、关键词提取去重/截断/停用词、handle 的 DAG 输入文件读取、LLM 成功/
  失败回退/非法枚举回落、analyze() 便捷函数

## [3.6.0] - 2026-08-24

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（内容生产能力提升）
- **fact_checker 事实核查 Agent（MVP）**：
  - 从最终文档提取数字类可验证声明（百分比/带单位数值/年份/版本号，上限可配），
    对照检索源做一致性核查：无 LLM 用归一化字符串匹配（零成本基线），有 LLM 用
    批量语义判定（supported/refuted/unverifiable），LLM 失败自动回退字符串匹配
  - 未核实声明在文档尾部附加「事实核查附注」（明确标注：启发式核查，
    unverifiable ≠ 错误），核查报告同时写入节点结果供 API/MCP 消费
  - 新增 `pipelines/docgen-verified.yaml`（8 层 DAG：checker → fact_checker → layout）；
    **默认 docgen 流水线零改动**
- **主流 LLM 供应商预置**：llm_router 新增 openai / deepseek / moonshot / qwen 四个
  OpenAI 兼容供应商定义（此前仅国内二线云厂商），`.env.example` 补三行组配置示例；
  Claude 原生接口非 OpenAI 格式，已在模板中说明经兼容网关接入

### Fixed（降级透明化）
- **空章节不再静默交付**：writer 无 LLM 路径下未能填充内容的章节，现在会在文档头部
  插入「⚠️ 降级声明」块列出章节名与修复建议，CLI 渲染时同步输出 stderr 警告，
  result.stats 带 empty_sections 字段供程序化消费
- 移除 run.py 中一段历史遗留的不可达死代码（three_pass 分支内 except 块后的 ascii-fix）

### Removed（Breaking）
- **移除已废弃的 ThreePassPipeline**（该模块自带的 DeprecationWarning 声明
  "不再维护，将在未来版本移除"）：删除模块、`--three-pass` CLI 参数、包导出。
  迁移：`--pipeline docgen` 已覆盖同等能力且更完善；`--three-pass` 现在打印迁移指引并 exit 2
- 删除 config.json / config.production.json 中零消费的死配置键 `writer.template_dir`
  （writer 实际使用内置骨架模板，templates 目录从未存在）

### 测试
- 新增 tests/test_fact_checker.py（13 项）：声明提取/来源匹配/嵌套来源收集/
  LLM 失败回退/附注渲染；docgen-verified.yaml 解析与 fact_checker 注册端到端验证

## [3.5.0] - 2026-08-24

### Fixed（第九轮审查修复：API 契约 / 鉴权可用性 / MCP / CICD）

**鉴权与 Dashboard 可用性**
- **鉴权本机信任模式**：未配置 `ADMIN_API_KEY` 时，绑定回环地址免鉴权访问
  （此前所有受保护端点无条件 401，Dashboard 永远空白且无任何提示）；
  绑定非回环地址 + 无 key → **拒绝启动**（安全门），与 docs/api.md 原有描述对齐
- Dashboard 前端：请求携带 Bearer Token（localStorage）；401 时弹出 Token 输入框自动重试
- run.py 消费配置文件的 `admin_api.host/port`（此前该配置块整体无效，
  config.production.json 的 `0.0.0.0` 绑定从未生效）

**API 契约修复**
- **OpenAPI 安全声明反转修正**：新增全局 `security: [BearerAuth]`；
  `/stream` 移除错误的 `security: []`（实际需鉴权）；`/health` 显式豁免——
  此前整份 Spec 将全部端点描述为公开，与实现完全相反
- **版本管理端点接线**：`_handle_versions_list/diff/rollback/stats` 四个 handler
  已实现但从未注册路由（全部 404 死代码）→ 现已接入 do_GET/do_POST 并补 OpenAPI 定义
- 错误响应信封统一为 `{"error": ...}`（消除三种顶层结构并存）
- `cancel/pause/resume/rerun` 任务不存在时返回 **404**（此前 200 + false 无法区分）
- `/api/dashboard`、`/api/pipeline`、`/stream/metrics`、versions 四端点补入 OpenAPI；
  TaskSubmit schema 补 `output` 参数

**前端字段契约修复（Dashboard 三处恒错数据显示）**
- Queue Depth 改读 `/health` 顶层 `queue_depth`（原读 metrics 子对象不存在的字段，恒 0）
- DB Store 显示 `store.messages` 条目数 + `db_size` MB（原读不存在字段恒显示 "6 entries"）
- 任务列表改用 `/api/dashboard` 聚合端点，进度条真实生效
  （原读 `/tasks` 列表不存在的 progress/steps 字段，恒 0%）
- 部分请求失败时状态栏显示"⚠ 部分数据不可用"角标（原先静默渲染空数据）

**其他修复**
- MCP Server `get_pipeline_info` 必失败修复：改读 `plan.levels`
  （原访问不存在的 `plan.execution_order/dag_nodes` 属性，每次调用 -32603）；
  SERVER_VERSION 动态取包版本（原硬编码 3.2.0）
- SSE 流水线线程 `worker.join()` 补 120s 超时（原无限阻塞可耗尽 HTTP 线程）
- pause() 不再在节点并发执行中途保存撕裂 checkpoint；改为执行循环在暂停边界
  （上一 level 完结后）保存一致性快照；语义已在 docstring 文档化
- POST 路由先鉴权后读请求体；task_id 路径参数接入 `_validate_task_id` 校验
- 删除死模块 `batch_queue.py`（167 行语句零调用方）

### Changed（CICD）
- **perf-regression job 从形同虚设变为真实回归门**：
  baseline 经 actions/cache 在运行间传递 + benchmark.py 对比通过后滚动更新基线
  （此前 baseline 被 gitignore 导致 CI 永远走"无 baseline 跳过检测"分支）

### 测试
- 新增 `tests/test_auth_contract.py`（15 项）：真实 HTTP 层鉴权矩阵、
  versions 路由接线、404 语义、OpenAPI 安全契约、MCP get_pipeline_info

## [3.4.0] - 2026-08-22

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（进程执行模式正式支持）
- **`executor_type: process` 从实验性限制变为可用特性**——子进程上下文自动重建：
  - `DAGExecutor` 新增 `child_context` 配置（agents_dir / agent_names / config，
    由 `PipelineOrchestrator.register_agents()` 写入），纯数据、可 pickle
  - `_execute_node_worker` 在 worker 进程内依据 child_context 一次性重建
    Registry（关闭健康检查线程）与非持久化 MessageBus，经 AgentLoader 加载 Agent；
    每个worker 进程仅重建一次（模块级缓存）
  - `__getstate__` 补齐剥离全部含线程锁组件（熔断器/限流器/Metrics/查询缓存），
    修复此前 DAGExecutor 整体 pickle 必然失败导致节点回落父进程重试的问题；
    `__setstate__` 对剥离组件按需重建可用实例
- 新增 `tests/test_process_mode.py`：pickle 往返、无 context 守卫报错、
  **真实 ProcessPoolExecutor 跨进程执行**（探针 Agent 返回子进程 pid ≠ 父进程 pid）
- 文档：docs/architecture.md §5 更新为进程模式工作原理 + 已知限制

### Changed
- executor_factory 进程模式告警更新：不再称"实验性/必然失败"，改为说明序列化开销与隔离限制

## [3.3.3] - 2026-08-22

### Changed（剩余技术债清零）
- **边缘超长方法拆分**（上轮 150L 边缘项全部处理，全仓 >150L 函数归零）：
  - `run.py:main` 199L → ~110L（提取 `build_arg_parser`）
  - `run.py:_run_single_task` 157L → ~60L（提取任务摘要/进度轮询/步骤收集/
    输出路径解析/结果渲染 5 个函数）
  - `pipeline.py:run_plan_async` 153L → ~85L（池化合并复用 `_merge_pooled_results`；
    收尾提取为 `_finalize_plan_task_async`，与同步版差异保留：不更新 task_queue、不发事件钩子）
  - `scripts/safe_writer.py:safe_write` 161L → ~90L（提取备份+manifest/换行符处理/
    临时文件写入/体积行数校验 4 个函数）
  - `admin_api.py:_handle_stream` 167L → ~80L（提取 SSE 帧发送/重连 replay/
    writer 查找/后台流水线线程 4 个方法）
- **.dockerignore 补全**：新增 checkpoints/versions/backups/cache/bus_data/
  .pytest_tmp/.test_checkpoints/.test_outputs/.zcode（运行时产物不进镜像构建上下文）

### Fixed
- mypy：run.py 提取函数的 Any 返回值显式 str() 收敛（3 处 no-any-return）

## [3.3.2] - 2026-08-22

### Changed（技术债清理）
- **超长方法拆分**（行为不变，582 测试全过）：
  - `writer.py:_restructure_document` 237L → 57L（提取 prompt 增强/素材构建/异步生成/
    Mermaid 修复/拼接装配等 7 个方法；顺带删除无调用的死代码嵌套函数 `_llm_generate`）
  - `pipeline.py:run_plan` 192L → 116L（提取 `_merge_pooled_results` / `_finalize_plan_task`）
  - `dag_executor.py:execute_level` 190L → 62L、`execute_level_async` 178L → 瘦身
    （共享提取：业务失败判定/重试结果评估/节点成功落账/futures 提交/同步与异步重试循环/步骤记录，
     两版本 finally 记录逻辑去重为单一 `_record_step_result`）
  - `openapi_spec.py:generate_spec` 267L → ~30L（按 section 拆为模块级函数；
     生成结果经 JSON diff 验证字节级一致）
- **Dashboard XSS 加固**：app.js 全部 6 处 innerHTML 字符串拼接改为
  createElement/textContent DOM 构建，动态数据不再经过 HTML 解析；
  移除不再需要的 escapeHtml 辅助函数
- **文档补全**：新增 docs/api.md（REST API 参考）、docs/architecture.md（分层架构 +
  线程模型 + 数据流）、docs/agents.md（Agent 开发指南 + 沙箱规则）；README 增加文档索引

### Fixed
- mypy：run_plan 提取后 `combined` 字典失去上下文推断导致的 2 个新错误（显式标注）；
  dag_executor 辅助函数返回类型收紧（原代码靠 type: ignore 压制）

## [3.3.1] - 2026-08-22

### Fixed
- **writer.py 生产 bug**：`_restructure_document` 的 `asyncio.gather` 在事件循环外调用，
  配置 LLM Key 且走同步调用路径时必然抛 `RuntimeError`（无 current event loop，协程未 await）。
  修复为在运行中的 loop 内执行 gather；同时移除集成测试中掩盖该缺陷的 `contextlib.suppress`
- scripts/format_converter.py: 清零 18 个 mypy 类型错误（str/Path 混用、Optional 未收窄），行为不变
- llm_router.py: 注册 atexit 钩子关闭共享 aiohttp Session，消除连接泄漏告警
- Dockerfile: 显式创建并 chown checkpoints/logs/versions/backups 目录，补全 VOLUME 声明
  （修复匿名卷 root 属主隐患；versions/backups 数据不再随容器销毁丢失）
- 文档：README/deployment.md 与 config.json 实际搜索引擎列表对齐（原 mock 描述过时）；
  Python 版本要求统一为 3.11+（与 pyproject 一致）；测试计数更新为 582；
  生产配置对比表与 config.production.json 实际内容对齐

### Changed
- CI: perf-regression 在 push main 时也触发（原来仅 PR）；dev 依赖安装加版本下界（对齐 pyproject dev extras）
- run.py: `_resolve_pipeline_plan` 返回类型精确为 `tuple[Any, bool]`

## [3.3.0] - 2026-08-11

### Fixed
- 修复全部140项审计问题（24 P0 + 58 P1 + 58 P2）
- P0: pipeline.py run() NameError、run_steps清理、PipelineTask pickle化
- P0: dag_executor.py fail_fast软中断
- P0: message_bus_v3.py 超时竞态、幂等原子性、DLQ处理
- P0: task_queue.py fd泄漏(threading.local)、total_changes回归
- P0: circuit_breaker.py HALF_OPEN CAS原子递增、回调移出锁外
- P0: rate_limiter.py Condition关联锁、notify_all
- P0: cache_manager.py 回填用文件原始ts、双重record_set修复
- P0: llm_router.py aiohttp timeout、异常吞没
- P0: search_engines.py 异常吞没、CacheManager.put→set、线程安全
- P0: admin_api.py /stream鉴权、webhook SSRF防护、output路径白名单
- P0: checkpoint_manager.py task_id路径遍历校验
- P0: version_manager.py rollback路径白名单
- P0: agent_loader.py AST沙箱增强(ImportFrom+裸名黑名单)
- P0: quality_gate.py 专有名词覆盖率阈值策略
- P0: safe_writer_agent.py _current_payload初始化、handle_writer_done写入
- P0: checker.py 移除重复订阅
- P0: researcher.py _search_manager属性名修复
- P0: run.py YAML加载失败友好退出、SIGTERM handler、局部导入修复
- P1: observability.py log put阻塞→put_nowait
- P1: event_hook.py 冗余_ensure_webhook_engine
- P1: registry.py _check_respawn持锁外部调用
- P1: quality_feedback.py busy_timeout PRAGMA
- P1: document_enhancer.py 空内容回退
- P1: three_pass_pipeline.py ThreadPoolExecutor(0)边界
- P1: benchmark.py 除零保护
- P1: scripts/markdown_checker.py _check_structure修复
- P1: scripts/safe_writer.py checksum自引用修复、file_checksum异常处理
- P1: scripts/convert_ascii.py ASCII_TREE_PATTERN乱码修复
- P1: scripts/format_converter.py 列表项<ul>包裹、mermaid重名

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- 新增10个测试模块（test_llm_router, test_search_engines, test_quality_gate_scoring, test_run, test_benchmark, test_markdown_checker, test_safe_writer, test_layout_optimizer, test_convert_ascii, test_format_converter）
- 525个测试全部通过

## [3.2.0] - 2026-08-08

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- 成本追踪/告警/质量闭环/MCP Server/Agent沙箱/集成测试

## [3.1.0] - 2026-08-06

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- async I/O + orjson + SSE reconnect + fast_json module
- PEV-ready API extensions + EventHook system
- /stream endpoint with end-to-end async pipeline
- run_plan_async + on_stop lifecycle hook
