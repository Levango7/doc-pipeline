"""
VersionManager - 文档版本管理
================================
在 SafeWriter 备份机制基础上提供正式的版本管理能力：
  - 自动递增版本号（v1, v2, v3...）
  - 任意两版本间 diff 对比（unified diff 格式）
  - 按版本号回滚
  - 版本元数据（时间戳、任务ID、质量评分、内容哈希）
  - 线程安全

用法：
    from pipeline_core.version_manager import VersionManager

    vm = VersionManager(versions_dir="versions")
    ver = vm.commit("output/doc.md", content, task_id="abc", quality_score=82.5)
    print(ver)  # {"version": 3, "path": "...", ...}

    history = vm.history("output/doc.md")
    diff = vm.diff("output/doc.md", v1=2, v2=3)
    vm.rollback("output/doc.md", version=2)
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class VersionIndexCorrupted(Exception):
    """版本索引损坏（已重命名 .corrupt-* 保留现场，禁止据此清理版本内容）"""


def _atomic_write_text(path: Path, content: str):
    """临时文件 + os.replace 原子写文本，避免半写文件"""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise

# ─── 安全防护辅助 ─────────────────────────────
# 安全修复 (P0): rollback 路径白名单校验，防止任意文件写入


def _validate_file_path(path_str: str, base_dir: str = None) -> tuple[bool, str]:
    """校验文件路径必须落在 base_dir（默认 cwd）范围内。"""
    if not path_str:
        return False, "路径为空"
    base = Path(base_dir or os.getcwd()).resolve()
    try:
        target = Path(path_str).resolve()
    except (OSError, ValueError) as e:
        return False, f"路径解析失败: {e}"
    try:
        target.relative_to(base)
    except ValueError:
        return False, f"路径 {path_str!r} 不在允许目录 {base!s} 内"
    return True, ""


@dataclass
class VersionEntry:
    """单个版本记录"""
    version: int
    file_path: str              # 原始文件路径
    content_path: str           # 版本内容存储路径
    sha256: str                 # 内容哈希
    size: int                   # 内容字节数
    lines: int                  # 行数
    created_at: float           # 时间戳
    task_id: str = ""           # 关联任务ID
    quality_score: float = 0.0  # 质量评分
    message: str = ""           # 版本说明

    def to_dict(self) -> dict:
        return asdict(self)


class VersionManager:
    """文档版本管理器

    Args:
        versions_dir: 版本存储根目录
        max_versions: 每个文件最大保留版本数（超出后清理最旧的）
    """

    def __init__(self, versions_dir: str = "versions", max_versions: int = 50):
        self._root = Path(versions_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_versions = max_versions
        self._lock = threading.RLock()

    def _file_key(self, file_path: str) -> str:
        """将文件路径转为安全的目录名"""
        resolved = str(Path(file_path).resolve())
        return hashlib.sha256(resolved.encode()).hexdigest()[:16]

    def _file_dir(self, file_path: str) -> Path:
        """获取文件的版本存储目录"""
        d = self._root / self._file_key(file_path)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_path(self, file_path: str) -> Path:
        """版本索引文件路径"""
        return self._file_dir(file_path) / "index.json"

    def _load_index(self, file_path: str) -> list[dict]:
        """加载版本索引；损坏时保留现场并抛 VersionIndexCorrupted（绝不静默返回空）"""
        idx_path = self._index_path(file_path)
        if not idx_path.exists():
            return []
        try:
            with open(idx_path, encoding="utf-8") as f:
                return json.load(f)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError) as e:
            corrupt_path = idx_path.with_name(
                f"{idx_path.name}.corrupt-{int(time.time())}"
            )
            try:
                os.replace(str(idx_path), str(corrupt_path))
            except OSError:
                corrupt_path = idx_path
            raise VersionIndexCorrupted(
                f"版本索引损坏，已重命名为 {corrupt_path.name}: {e}"
            ) from e

    def _save_index(self, file_path: str, index: list[dict]):
        """保存版本索引（原子写）"""
        _atomic_write_text(
            self._index_path(file_path),
            json.dumps(index, ensure_ascii=False, indent=2),
        )

    def _max_disk_version(self, file_dir: Path, suffix: str) -> int:
        """扫描磁盘上已存在的内容文件，返回最大版本号"""
        max_v = 0
        for p in file_dir.glob(f"v*{suffix}"):
            stem = p.stem[1:]
            if stem.isdigit():
                max_v = max(max_v, int(stem))
        return max_v

    def _next_version(self, index: list[dict]) -> int:
        """计算下一个版本号"""
        if not index:
            return 1
        return max(entry["version"] for entry in index) + 1  # type: ignore[no-any-return]

    def commit(self, file_path: str, content: str,
               task_id: str = "", quality_score: float = 0.0,
               message: str = "") -> dict:
        """提交新版本

        Args:
            file_path: 文档路径
            content: 文档内容
            task_id: 关联任务ID
            quality_score: 质量评分
            message: 版本说明

        Returns:
            版本信息 dict
        """
        with self._lock:
            safe_mode = False
            try:
                index = self._load_index(file_path)
            except VersionIndexCorrupted as e:
                # 索引不可信 → 安全模式：只增不清理，绝不 unlink 旧版本内容
                logger.warning("版本索引损坏，进入安全模式（只增不清理）: %s", e)
                index = []
                safe_mode = True

            file_dir = self._file_dir(file_path)
            suffix = Path(file_path).suffix or ".md"
            version = self._next_version(index)
            if safe_mode:
                version = max(version, self._max_disk_version(file_dir, suffix) + 1)

            # 存储版本内容
            content_filename = f"v{version}{suffix}"
            content_path = file_dir / content_filename
            content_bytes = content.encode("utf-8")
            content_path.write_bytes(content_bytes)

            # 计算元数据
            sha256 = hashlib.sha256(content_bytes).hexdigest()
            lines = content.count("\n") + 1

            entry = VersionEntry(
                version=version,
                file_path=str(Path(file_path).resolve()),
                content_path=str(content_path),
                sha256=sha256,
                size=len(content_bytes),
                lines=lines,
                created_at=time.time(),
                task_id=task_id,
                quality_score=quality_score,
                message=message,
            )

            index.append(entry.to_dict())

            # 清理超出限制的旧版本（安全模式下跳过：索引不可信时不删除任何内容）
            if not safe_mode and len(index) > self._max_versions:
                removed = index[:len(index) - self._max_versions]
                index = index[len(index) - self._max_versions:]
                for old in removed:
                    old_path = Path(old["content_path"])
                    if old_path.exists():
                        old_path.unlink()

            self._save_index(file_path, index)
            return entry.to_dict()

    def history(self, file_path: str, limit: int = 20) -> list[dict]:
        """获取版本历史（最新在前）"""
        with self._lock:
            index = self._load_index(file_path)
            return list(reversed(index[-limit:]))

    def get_version(self, file_path: str, version: int) -> dict | None:
        """获取指定版本信息"""
        with self._lock:
            index = self._load_index(file_path)
            for entry in index:
                if entry["version"] == version:
                    return entry
            return None

    def get_content(self, file_path: str, version: int) -> str | None:
        """读取指定版本的内容"""
        entry = self.get_version(file_path, version)
        if not entry:
            return None
        content_path = Path(entry["content_path"])
        if not content_path.exists():
            return None
        return content_path.read_text(encoding="utf-8")

    def diff(self, file_path: str, v1: int, v2: int,
             context_lines: int = 3) -> str:
        """对比两个版本的差异（unified diff 格式）

        Args:
            file_path: 文档路径
            v1: 旧版本号
            v2: 新版本号
            context_lines: 上下文行数

        Returns:
            unified diff 字符串
        """
        content1 = self.get_content(file_path, v1)
        content2 = self.get_content(file_path, v2)

        if content1 is None:
            return f"错误: 版本 v{v1} 不存在或内容已清理"
        if content2 is None:
            return f"错误: 版本 v{v2} 不存在或内容已清理"

        lines1 = content1.splitlines(keepends=True)
        lines2 = content2.splitlines(keepends=True)

        diff_lines = difflib.unified_diff(
            lines1, lines2,
            fromfile=f"v{v1}",
            tofile=f"v{v2}",
            n=context_lines,
        )
        return "".join(diff_lines)

    def rollback(self, file_path: str, version: int) -> dict:
        """回滚到指定版本（将旧版本内容写回原文件，并创建新版本记录）

        Args:
            file_path: 文档路径
            version: 目标版本号

        Returns:
            {"status": "ok", "new_version": N, "rolled_back_to": version}
        """
        # 安全修复 (P0): file_path 路径白名单校验，防止任意文件写入
        ok, reason = _validate_file_path(file_path)
        if not ok:
            raise ValueError(f"无效的 file_path: {reason}")
        content = self.get_content(file_path, version)
        if content is None:
            return {"status": "error", "message": f"版本 v{version} 不存在或内容已清理"}

        target = Path(file_path)
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current is not None and current != content:
            # 写回前先把当前内容提交为新版本，保证写回目标永远存在于历史中
            self.commit(file_path, current, message="回滚前自动保存当前内容")

        # 原子写回原文件
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, content)

        # 创建新版本记录（标记为回滚）
        new_entry = self.commit(
            file_path, content,
            message=f"回滚到 v{version}",
        )

        return {
            "status": "ok",
            "new_version": new_entry["version"],
            "rolled_back_to": version,
            "file_path": str(target),
        }

    def latest_version(self, file_path: str) -> int:
        """获取最新版本号（无版本返回 0）"""
        with self._lock:
            index = self._load_index(file_path)
            if not index:
                return 0
            return max(entry["version"] for entry in index)  # type: ignore[no-any-return]

    def stats(self) -> dict:
        """版本管理统计"""
        total_files = 0
        total_versions = 0
        total_size = 0
        if self._root.exists():
            for d in self._root.iterdir():
                if d.is_dir():
                    idx = d / "index.json"
                    if idx.exists():
                        total_files += 1
                        try:
                            data = json.loads(idx.read_text(encoding="utf-8"))
                            total_versions += len(data)
                            total_size += sum(e.get("size", 0) for e in data)
                        except Exception:
                            pass
        return {
            "tracked_files": total_files,
            "total_versions": total_versions,
            "total_size_bytes": total_size,
            "versions_dir": str(self._root),
            "max_versions_per_file": self._max_versions,
        }


# ─── 全局单例 ──────────────────────────────

_vm_instance: VersionManager | None = None
_vm_lock = threading.Lock()


def get_version_manager(versions_dir: str = "versions") -> VersionManager:
    """获取全局 VersionManager 单例"""
    global _vm_instance
    if _vm_instance is None:
        with _vm_lock:
            if _vm_instance is None:
                _vm_instance = VersionManager(versions_dir=versions_dir)
    return _vm_instance
