"""SafeWriter Agent v2 - 安全写入插件"""
import time
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from datetime import datetime
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline_core.base_agent import BaseAgent, Message, AgentStatus, AgentMeta

AGENT_NAME = "safe_writer"
AGENT_VERSION = "2.0"
AGENT_DESC = "安全写入 Agent - 备份+验证+原子替换+过期清理"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 99
INPUT_TOPICS = ["writer.done", "layout.done", "safe_writer.write", "safe_writer.input", "safewriter.input"]
OUTPUT_TOPICS = ["safe_writer.done", "safe_writer.failed"]
DEPENDENCIES = ["layout"]
CACHE_TTL = 0
RESPAWN = False


class SafeWriterAgent(BaseAgent):
    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self.pending: dict = {}
        self._default_backup_dir = config.get("backup_dir", "backups")
        self._manifest_lock = threading.Lock()
        self.log_info(f"SafeWriter v{AGENT_VERSION} 初始化完成")

    def handle(self, msg: Message) -> dict | None:
        self.report(AgentStatus.RUNNING, "准备写入...")
        payload = msg.payload
        task_id = payload.get("task_id", "")
        content = payload.get("content", "")
        target = payload.get("target", payload.get("target_file", ""))
        backup_dir = payload.get("backup_dir", self._default_backup_dir)
        reason = payload.get("reason", "plugin")
        if not target:
            return {"status": "error", "message": "未指定目标文件"}
        if not content:
            return {"status": "error", "message": "内容为空"}
        result = self._safe_write(target, content, backup_dir, reason, task_id)
        self.publish("safe_writer.done" if result.get("status") == "ok" else "safe_writer.failed",
                     {"task_id": task_id, "target": target, "result": result})
        return result

    def _safe_write(self, target: str, content: str, backup_dir: str, reason: str, task_id: str) -> dict:
        import os, shutil, hashlib, json
        from datetime import datetime
        target = str(Path(target).resolve())
        backup_dir = str(Path(backup_dir).resolve())
        manifest_path = Path(backup_dir) / "manifest.json"
        info = self._get_info(target)
        self.log_info(f"目标: {target}")
        backup_path = None
        if info.get("exists"):
            backup_path = str(Path(backup_dir) / f"{Path(target).stem}_{datetime.now():%Y%m%d_%H%M%S}{Path(target).suffix}")
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_path)
            self.log_info(f"备份: {Path(backup_path).name} ({info['size']:,} bytes)")
        enc = "utf-8-sig" if Path(target).suffix.lower() in {".csv", ".tsv"} else "utf-8"
        # 确保目标目录存在
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = str(Path(target).parent or ".")
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=Path(target).suffix, prefix=".tmp_", dir=tmp_dir)
        os.close(fd)
        try:
            try:
                content_bytes = content.encode(enc)
                with open(tmp_path, "wb") as f:
                    f.write(content_bytes)
                new_size = len(content_bytes)
                new_lines = content.count("\n") + 1
                self.log_info(f"写入临时: {new_size:,} bytes, {new_lines} 行")
            except Exception as e:
                return {"status": "error", "message": f"写入失败: {e}"}
            issues = []
            if len(content.strip()) == 0:
                issues.append("P0: 内容为空")
            if info.get("exists") and new_size > 0:
                r = new_size / info["size"]
                if r < 0.5:
                    issues.append(f"P1: 文件<50% ({info['size']:,}->{new_size:,})")
                if r > 5.0:
                    issues.append(f"P1: 文件>5倍 ({info['size']:,}->{new_size:,})")
            if issues:
                for iss in issues:
                    self.log_warning(f"验证失败: {iss}")
                return {"status": "error", "issues": issues, "backup": backup_path}
            try:
                os.replace(tmp_path, target)
                tmp_path = None
                with self._manifest_lock:
                    man = self._load_manifest(manifest_path)
                    rel = target
                    if rel not in man["files"]:
                        man["files"][rel] = {"backups": [], "latest": None}
                    man["files"][rel]["backups"].append({
                        "path": backup_path,
                        "timestamp": datetime.now().isoformat(),
                        "size": info.get("size", 0),
                        "reason": reason,
                        "agent": self.name,
                        "task_id": task_id,
                    })
                    man["files"][rel]["latest"] = man["files"][rel]["backups"][-1]
                    self._save_manifest(manifest_path, man)
                self._cleanup(backup_dir, manifest_path)
                self.log_info(f"完成: {target} ({new_size:,} bytes)")
                return {"status": "ok", "backup": backup_path, "size": new_size, "lines": new_lines}
            except Exception as e:
                self.log_error(f"替换失败: {e}")
                return {"status": "error", "message": str(e)}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _get_info(self, path):
        if not Path(path).exists():
            return {"exists": False}
        st = os.stat(path)
        size = st.st_size
        md5 = hashlib.md5()
        lines = 1
        with open(path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                md5.update(chunk)
                lines += chunk.count(b"\n")
        return {"exists": True, "size": size, "lines": lines, "md5": md5.hexdigest()}

    def _load_manifest(self, path):
        if not Path(path).exists():
            return {"files": {}, "version": "2.0"}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_manifest(self, path, data):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _cleanup(self, backup_dir: str, manifest_path: Path):
        import time
        ttl_days = self.config.get("backup_ttl_days", 7)
        max_backups = self.config.get("max_backups", 20)
        cutoff = time.time() - ttl_days * 86400
        try:
            # 1. TTL 过期清理
            for f in Path(backup_dir).iterdir():
                if f.name == "manifest.json" or f.suffix == ".json":
                    continue
                if f.is_file() and f.stat().st_mtime < cutoff:
                    os.remove(f)
                    self.log_info(f"清理过期: {f.name}")

            # 2. 数量超限清理（保留最新的 max_backups 个）
            remaining = sorted(
                [f for f in Path(backup_dir).iterdir()
                 if f.is_file() and f.name != "manifest.json" and f.suffix != ".json"],
                key=lambda f: f.stat().st_mtime, reverse=True
            )
            while len(remaining) > max_backups:
                stale = remaining.pop()
                os.remove(stale)
                self.log_info(f"数量超限清理: {stale.name}")
        except Exception as e:
            self.log_error(f"清理失败: {e}")

    def handle_writer_done(self, msg: Message):
        payload = msg.payload
        task_id = payload.get("task_id", "")
        content = payload.get("content", "")
        target = payload.get("target_file", "")
        if not target:
            target = self.config.get("default_target", "")
        self.pending[task_id] = {"content": content, "target": target, "timestamp": time.time()}