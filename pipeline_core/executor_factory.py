"""执行器工厂 —— 根据配置创建 ThreadPoolExecutor 或 ProcessPoolExecutor。

配置方式：
  pipeline.yaml 中:
    execution:
      executor_type: thread   # "thread" (默认) 或 "process"

选择建议：
  - thread (默认): I/O 密集型场景（HTTP 请求、LLM 调用、文件读写）
    优势：零序列化开销、共享内存、向后兼容
  - process: CPU 密集型场景（大规模 TF-IDF、并行 LLM 生成、数值计算）
    优势：绕过 GIL、真正并行、水平扩展
    注意：提交的函数必须可 pickle（模块级函数或用 functools.partial）
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from typing import Union


def create_executor(max_workers: int = 4, executor_type: str = "thread") -> Union[ThreadPoolExecutor, ProcessPoolExecutor]:
    """根据类型创建执行器。

    Args:
        max_workers: 最大 worker 数量
        executor_type: "thread" 或 "process"

    Returns:
        ThreadPoolExecutor 或 ProcessPoolExecutor 实例
    """
    if executor_type == "process":
        return ProcessPoolExecutor(max_workers=max_workers)
    return ThreadPoolExecutor(max_workers=max_workers)


def is_process_executor(executor) -> bool:
    """判断是否为 ProcessPoolExecutor"""
    return isinstance(executor, ProcessPoolExecutor)
