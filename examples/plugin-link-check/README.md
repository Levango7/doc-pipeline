# doc-pipeline-plugin-link-check —— 外部插件 Agent `link_check`

链接清单抽取（不发起请求）。通过 `doc_pipeline.agents` entry_points 接入：装好即被本仓零改动地
发现 / 注册 / 执行（product-spec §2.1 验收线第 2 条）。

## 契约

- entry point 的**值必须是模块路径**，group 固定 `doc_pipeline.agents`；
- 模块顶层声明 `AGENT_NAME` 并至少定义一个 `BaseAgent` 子类（与内置
  `agents/*.py` 同构）；
- 加载器做同一套 AST 安全检查：不碰黑名单调用就不需要 `SANDBOX_TRUSTED`
  （那只留给随产品发布的内置件）；
- 同名冲突时本仓内置件优先。

## 试一下

```bash
pip install ./examples/plugin-link-check
python run.py --list-agents      # 看到 link_check 带 [entry_point:link_check] 标记
```

之后可当任意流水线节点用：`pipelines/*.yaml` 的 `agents:` 里写 `name: link_check`。
不想要了就 `pip uninstall doc-pipeline-plugin-link-check`。

## 验证

`tests/test_plugins_ecosystem.py` 会真建 venv、真装六个包，逐个断言被发现 /
注册 / 执行，来源标记 `entry_point:link_check`；快判据另查本包的 group、模块形状
与 AST 安全。总览见 [../README.md](../README.md)。
