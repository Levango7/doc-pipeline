"""节点名 ↔ Agent 名的唯一解析处。

DAG 里一个节点的名字同时承担两件事：**图里的身份**（每个节点必须唯一，池化时是
`writer_pool_0`）和**由哪个 Agent 执行**（`writer`）。此前"从身份还原 Agent"这件事
以 `split("_pool_")[0]` 的形式散落在 13 处，各自实现略有差别——想支持子流水线内联
（同一 Agent 在一棵图里出现多次）和 foreach（同一节点按项展开）就必须先把它收敛成
一个地方，否则加一种命名形态就要改 13 个文件。

命名约定（`{agent}[_pool_{i}][__{alias}]`）：

    writer              单实例，无池、无别名
    writer_pool_2       池化第 2 个实例
    writer__review      由 docgen-review 子流水线内联进来的 writer 节点

反向解析用 `agent_of()`；`pool_index_of()` 取池下标；`alias_of()` 取内联别名。
"""
from __future__ import annotations

POOL_SEP = "_pool_"
ALIAS_SEP = "__"


def agent_of(node_name: str) -> str:
    """节点名 → 执行它的 Agent 名（剥掉池下标与内联别名）。"""
    return node_name.split(POOL_SEP)[0].split(ALIAS_SEP)[0]


def pool_index_of(node_name: str) -> int | None:
    """池下标；非池化节点返回 None。"""
    tail = node_name.split(ALIAS_SEP)[0]
    if POOL_SEP not in tail:
        return None
    _, _, idx = tail.partition(POOL_SEP)
    try:
        return int(idx)
    except ValueError:
        return None


def family_of(node_name: str) -> str:
    """去掉池下标后的"同一逻辑节点"标识——内联别名必须原样保留。

    `writer_pool_0` / `writer` → `writer`；`writer_pool_0__review` → `writer__review`；
    嵌套内联 `checker__b__via_nest` 保持自身不变。

    为什么需要它与 `agent_of` 并存：分组"同一节点的池兄弟"要按**家族**归并，
    而查注册表元信息要按 **Agent** 查。只留一个就会出错——只用 agent_of 做分组，
    内联进来的节点会掉出上游闭包（实测过：链式 call 的下游拿到的是两跳之前的
    内容，中间那跳被静默丢了）。
    """
    head, _, rest = node_name.partition(ALIAS_SEP)
    head = head.split(POOL_SEP)[0]
    return f"{head}{ALIAS_SEP}{rest}" if rest else head


def alias_of(node_name: str) -> str:
    """内联别名（子流水线展开时用来保证节点身份唯一）；无别名返回空串。

    注意只取第一段：`writer__review__deep` 的别名是 `review`，剩下的 `deep`
    归下一层解析——别名段本身不再参与 Agent 名还原。
    """
    parts = node_name.split(ALIAS_SEP)
    return parts[1] if len(parts) > 1 else ""


def node_id(agent_name: str, pool_index: int | None = None,
            alias: str = "") -> str:
    """按约定拼出节点身份（agent_of 的正向构造）。"""
    name = agent_name if pool_index is None else f"{agent_name}{POOL_SEP}{pool_index}"
    return f"{name}{ALIAS_SEP}{alias}" if alias else name
