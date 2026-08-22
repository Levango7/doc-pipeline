# Agent 开发指南

本文说明如何为 doc-pipeline 编写自定义 Agent。所有内容以当前代码为准：
契约定义见 `pipeline_core/base_agent.py`，加载与沙箱见 `pipeline_core/agent_loader.py`。

---

## 1. Agent 模块契约

每个 Agent 是 `agents/` 目录下的一个 `.py` 文件（文件名 = Agent 名）。
加载器会读取模块级常量作为注册元信息：

| 常量 | 类型 | 说明 |
|------|------|------|
| `AGENT_NAME` | str | Agent 名称（须与文件名一致） |
| `AGENT_VERSION` | str | 版本号 |
| `AGENT_DESC` | str | 一句话描述 |
| `AGENT_AUTHOR` | str | 作者 |
| `AGENT_PRIORITY` | int | 订阅优先级（越小越先） |
| `INPUT_TOPICS` | list[str] | 订阅的消息主题（自动挂到 MessageBus） |
| `OUTPUT_TOPICS` | list[str] | 输出主题（文档用途） |
| `DEPENDENCIES` | list[str] | 依赖的其他 Agent |
| `CACHE_TTL` | int | 缓存 TTL 秒数 |
| `RESPAWN` | bool | 异常退出后是否自动重生 |
| `SUPPORTS_REGENERATION` / `REGENERATION_TARGET` / `REGENERATION_RECHECK` | — | 质量门控重生成循环的可选配置（见 `agents/quality_gate.py`） |

## 2. BaseAgent 必须实现的方法

继承 `pipeline_core.base_agent.BaseAgent` 并实现唯一的抽象方法：

```python
def handle(self, msg: Message) -> dict | None:
    """处理一条消息，返回结果 dict（写入任务输出）或 None"""
```

`BaseAgent.__init__(name, meta, config, message_bus=None, registry=None)` 由加载器调用，
子类一般不需要覆盖；如覆盖请先 `super().__init__(...)`（基类负责缓存目录、日志、
自动订阅 `INPUT_TOPICS`）。

## 3. 生命周期钩子（均可选覆盖）

| 钩子 | 触发时机 |
|------|---------|
| `on_start()` / `on_stop()` | Agent 启动 / 停止（`on_stop` 中应释放资源） |
| `on_pause()` / `on_resume()` | 流水线暂停 / 恢复（断点续传） |
| `on_config_update(changed_keys)` | `POST /api/config/reload` 配置热更新 |
| `on_snapshot()` / `on_restore(state)` | checkpoint 创建 / 恢复（断点续传状态） |
| `cleanup_task_temp(task_id)` / `cleanup_stale_temp(max_age_hours)` | 任务结束 / 启动时的临时文件清理（返回清理数量） |
| `is_healthy()` | Registry 健康检查 |

## 4. 消息与工具方法

`handle()` 内可用的基类辅助方法（详见 `pipeline_core/base_agent.py`）：

- `self.publish(topic, payload)` — 发布消息到总线
- `self.send_to(to_agent, topic, payload)` — 定向发送
- `self.reply(original_msg, payload)` — 回复请求
- `self.cache_get(key)` / `self.cache_set(key, data)` — 文件后端跨进程缓存
- `self.log_debug/info/warning/error(msg)` — 结构化日志
- `self.report(status, info)` — 上报状态到 Registry

## 5. 安全沙箱

第三方 Agent 加载时执行 AST 静态检查（`pipeline_core/agent_loader.py:_check_safety`），
内置白名单 Agent 跳过检查。默认 `strict_safety=True`，命中即阻断加载。

**危险调用黑名单**（`_DANGEROUS_CALLS`，节选）：
`os.system`、`os.popen`、`os.remove/unlink/rmdir/rename/chmod/kill/fork`、
`subprocess.Popen/run/call/check_call/check_output`、`eval`、`exec`、`compile`、
`__import__`、`shutil.rmtree/move/copy2`、`open`、`socket.socket/connect/bind`、
`ctypes.CDLL/PyDLL/WinDLL/pythonapi`、`pickle.loads/load`、`marshal.loads`。

**导入拦截**：
- `from subprocess import Popen` 等 `from <危险模块> import <危险名>` 直接拦截；
- `from os import system/remove/...` 拦截危险函数名；
- `import ctypes / pickle / marshal` 直接拦截。

即：自定义 Agent 内**不要用 `open()`**——需要读写文件时通过基类缓存或 config 提供的路径封装。

## 6. 最小示例

```python
"""agents/echo_agent.py — 最小自定义 Agent 示例"""
from pipeline_core.base_agent import AgentStatus, BaseAgent, Message

AGENT_NAME = "echo"
AGENT_VERSION = "1.0"
AGENT_DESC = "回显输入的演示 Agent"
AGENT_AUTHOR = "your-name"
AGENT_PRIORITY = 90
INPUT_TOPICS = ["echo.input"]
OUTPUT_TOPICS = ["echo.done"]
DEPENDENCIES = []
CACHE_TTL = 0
RESPAWN = False


class EchoAgent(BaseAgent):
    def handle(self, msg: Message) -> dict | None:
        payload = msg.payload if hasattr(msg, "payload") else {}
        text = payload.get("text", "")
        self.log_info(f"echo 收到: {text[:50]}")
        self.publish("echo.done", {"text": text})
        return {"status": "ok", "text": text}
```

放入 `agents/` 目录后，`python run.py input.md --agent echo` 或在 pipeline YAML 的
`agents:` 列表中声明即可被加载。若 Agent 未被流水线 DAG 引用，
可用 `--list-agents` 确认注册状态。
