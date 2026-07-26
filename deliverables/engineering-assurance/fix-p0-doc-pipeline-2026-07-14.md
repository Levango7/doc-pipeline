# doc-pipeline P0 缺陷修复报告

**日期**：2026-07-14
**工作流**：P0 缺陷修复（基于 2026-07-22 综合评审的 4 项 🔴 阻塞项）
**参与成员**：Cody（代码审查师，执行修复）/ 主理人甄宇航（直接复核与验证）

---

## 📌 TL;DR（执行摘要）

- 4 项 🔴 P0 缺陷已全部修复并落盘：密钥泄露、控制面未鉴权写入、事件循环阻塞、/stream 截断。
- 编译校验通过（`py_compile` 无报错）；测试 **169 passed / 1 failed**，唯一失败为环境缺失 `pytest-asyncio` 插件（与评审基线一致，非本次引入）。
- Dashboard SSE（`/stream`，22 个相关用例）仍全部通过 —— 鉴权收紧未破坏只读流式端点。
- 实施说明：实现 worker 在写入全部补丁后因框架工具 `TaskStop` 不可用而异常退出，但 4 处补丁均已成功写入文件；最终验证由主理人直接执行。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 整体评级 | 🟢 修复完成（无回归） |
| 阻塞项数量 | 0（4 项 P0 全部关闭） |
| 关键行动项 | 3 条（见下方行动清单） |
| 建议下一步 | 轮换可能已泄露的 `.env` 密钥；生产环境设置 `ADMIN_API_KEY`；补充 hook 注册 SSRF 校验 |

---

## 🔧 修复明细（按严重度）

| # | 严重度 | 类别 | 文件:行 | 问题 | 修复方式 | 来源 |
|---|--------|------|---------|------|---------|------|
| P0-1 | 🔴严重 | 密钥管理 | `.dockerignore`（新建） | Dockerfile `COPY . .` 将 `.env` 明文密钥打进镜像 | 新建 `.dockerignore`，排除 `.env`/`*.secret`/`config/secrets.yaml`/`output/`/`logs/`/`__pycache__/` 等 | Cody |
| P0-2 | 🔴严重 | 认证绕过 | `admin_api.py:161` | `_check_auth` 在 `api_key` 为空时 `return True` → 控制面写端点完全开放 | 改为 `return False`（默认关闭）；`/health` 与 `/stream` 在路由层豁免鉴权；`start()` 增加空密钥告警 | Cody |
| P0-3 | 🔴严重 | 性能/正确性 | `dag_executor.py:620` | `execute_level_async` 内 `task.stop_event.wait(delay)` 阻塞 asyncio 事件循环 | 改为 `await asyncio.sleep(delay)` 后显式检查 `task.stop_event.is_set()` | Cody |
| P0-4 | 🔴严重 | 数据完整性 | `admin_api.py:818` | `_handle_stream` 中 `worker.join(timeout=5)` 早退 → 孤儿线程、客户端收不到 `complete` | 改为 `worker.join()`（无超时），确保 worker 结束后才 `unregister_callback` | Cody |

---

### P0-2 路由调整细节（`admin_api.py` `do_GET`）
- 将 `/stream` 的鉴权豁免前移：`if self.path.startswith("/stream"): self._handle_stream(); return`（位于 `/health` 豁免之后、`_check_auth` 网关之前）。
- 删除原位于 `/api/pipeline` 之后的重复 `/stream` 路由分支，避免重复处理。
- `do_POST` / `do_DELETE` 维持原 `_check_auth()` 前置调用；配合 P0-2a 现在对未配置密钥的场景一律返回 `401`，写端点默认关闭。
- 保留 `/health` 与 `/stream` 的 `Access-Control-Allow-Origin: "*"`（Dashboard 跨域需要，属已知项，非本次范围）。

---

## ✅ 验证结果

- **语法编译**：`python -m py_compile pipeline_core/admin_api.py pipeline_core/dag_executor.py` → `COMPILE_OK`。
- **单元/集成测试**：`pytest -q` → **169 passed, 1 failed**。
  - 失败用例：`tests/test_pipeline_async.py::TestRunPlanAsync::test_run_plan_async_with_empty_plan` —— 报错 `async def functions are not natively supported`，缺 `pytest-asyncio` 插件。环境与评审基线一致，**非本次修改导致**。
  - SSE 流式用例 `tests/test_streaming_sse.py`（22 例）全部通过，证明 `/stream` 鉴权豁免有效。

---

## ✅ 行动清单（按优先级排序）

| # | 行动 | 负责角色 | 紧急度 | 预期完成 |
|---|------|---------|--------|---------|
| 1 | 若 `.env` 曾被用于构建/推送过镜像，**立即轮换其中所有密钥**（API Key、Token、数据库口令） | 人类运维负责人 | P0 | 立即 |
| 2 | 生产部署设置 `ADMIN_API_KEY` 环境变量以启用控制面鉴权；为空时写端点保持 401 关闭 | DevOps / SRE | P0 | 下次发布 |
| 3 | 补充 `/api/events/hooks` 注册的 SSRF 校验（拒绝指向 metadata `169.254.169.254` / 内网地址的 hook URL） | 代码审查师 | P1 | 下个迭代 |
| 4 | 安装 `pytest-asyncio` 使 async 用例可执行，消除环境型红测 | 测试专家 | P2 | 排期 |

---

## ⚠️ 待完善 / 已知局限

- 实施 worker（Cody）在写入全部补丁后，因本环境 `general-purpose` agent 不提供 `TaskStop` 工具而异常退出；补丁本身已全部落盘，验证由主理人直接完成。
- P0-2 的 hook URL SSRF 校验未在本轮实施（属评审中单独列出的 🟠 项），已列入行动清单 #3。
- CORS `*` 维持现状（Dashboard 依赖）；如需收紧，建议改为显式来源白名单（属独立优化项）。

---

## 📚 数据来源 & 成员产出索引

- Cody（代码审查师）原始产出：4 处补丁已写入 `pipeline_core/admin_api.py`、`pipeline_core/dag_executor.py`、`.dockerignore`（worker 末期框架异常，未返回汇总文本，由主理人核验文件确认）。
- 主理人甄宇航复核产出：`py_compile` + `pytest` 验证结果、本报告汇编。
- 基线参考：`deliverables/engineering-assurance/comprehensive-review-doc-pipeline-2026-07-22.md`（4 项 🔴 来源）。

---

> 本报告由工程保障团队 AI 协作生成，关键决策（尤其是密钥轮换与生产鉴权配置）请由人类工程负责人复核并执行。
