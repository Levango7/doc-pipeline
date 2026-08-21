"""
统一配置中心 —— 级联配置：默认值 → 配置文件 → 环境变量 → 代码覆盖
"""
from __future__ import annotations

import os
import threading
from pathlib import Path


class ConfigCenter:
    """统一配置中心 —— 级联配置：默认值 → 配置文件 → 环境变量 → 代码覆盖"""

    def __init__(self, config_file: str = None, auto_reload: bool = True):
        """
        config_file: 主配置文件路径（YAML/JSON），默认查找 doc-pipeline/config.yaml
        auto_reload: 是否自动检测文件变化并热加载
        """
        self._config_file = Path(config_file) if config_file else None
        self._auto_reload = auto_reload
        self._data: dict = {}
        self._mtime: float = 0
        self._lock = threading.RLock()
        self._overrides: dict = {}
        self._load()

    def _load(self):
        """加载配置文件"""
        # 1. 加载默认值
        defaults = {
            "bus": {"db_path": "bus_data/message_bus.db", "persistence": True},
            "checkpoint": {"dir": "checkpoints", "max_age_days": 7},
            "agents": {"dir": "agents", "cache_dir": "cache", "log_dir": "logs"},
            "execution": {"max_workers": 8, "fail_fast": True, "max_retries": 3},
            "quality_gate": {"min_score": 70, "max_regenerations": 3},
            "llm": {"api_key_env": "LLM_API_KEY", "model": "@cf/moonshotai/kimi-k2.6"},
        }
        self._data = defaults.copy()

        # 2. 加载配置文件
        if self._config_file and self._config_file.exists():
            self._merge_file(self._config_file)
            self._mtime = self._config_file.stat().st_mtime

        # 3. 环境变量覆盖（以 DOCPIPE_ 开头的环境变量）
        # 例如 DOCPIPE_LLM__API_KEY=xxx → data["llm"]["api_key"] = "xxx"
        self._merge_env()

    def _merge_file(self, path: Path):
        """合并 YAML/JSON 配置文件"""
        import json
        with open(path, encoding="utf-8") as f:
            if path.suffix in (".yaml", ".yml"):
                try:
                    import yaml
                    file_data = yaml.safe_load(f) or {}
                except ImportError:
                    raise ImportError(
                        f"PyYAML is required to load {path} but is not installed. "
                        "Install with: pip install pyyaml"
                    ) from None
            else:
                file_data = json.load(f) or {}
        self._deep_merge(self._data, file_data)

    def _merge_env(self):
        """合并 DOCPIPE_ 前缀的环境变量。双下划线表示嵌套：DOCPIPE_LLM__MODEL → llm.model"""
        for key, value in os.environ.items():
            if not key.startswith("DOCPIPE_"):
                continue
            parts = key[8:].lower().split("__")
            target = self._data
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = self._coerce(value)

    def _coerce(self, value: str):
        """类型转换：'true'→True, '123'→123, '3.14'→3.14, '[a,b]'→list, '{"k":"v"}'→dict"""
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        # JSON 容器：支持环境变量传入数组/对象，如 DOCPIPE_RESEARCHER__SEARCH_ENGINES='["mock"]'
        if value.startswith("[,{"):
            try:
                import json as _json
                parsed = _json.loads(value)
                if isinstance(parsed, (list, dict)):
                    return parsed
            except ValueError:
                pass
        return value

    def _deep_merge(self, base: dict, override: dict):
        """深度合并字典"""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def _check_reload(self):
        """检查配置文件是否变更，自动热加载"""
        if not self._auto_reload or not self._config_file or not self._config_file.exists():
            return
        mtime = self._config_file.stat().st_mtime
        if mtime > self._mtime:
            with self._lock:
                if mtime > self._mtime:  # 双重检查
                    self._load()
                    self._mtime = mtime

    def get(self, key: str, default=None):
        """获取配置值。key 用点分隔：'llm.model' → data['llm']['model']"""
        self._check_reload()
        with self._lock:
            parts = key.split(".")
            target = self._data
            for part in parts:
                if isinstance(target, dict):
                    target = target.get(part)  # type: ignore[assignment]
                else:
                    return default
            return target if target is not None else default

    def set(self, key: str, value):
        """运行时覆盖配置"""
        with self._lock:
            parts = key.split(".")
            target = self._data
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value

    def to_dict(self) -> dict:
        """导出全部配置"""
        self._check_reload()
        with self._lock:
            import copy
            return copy.deepcopy(self._data)

    def reload(self):
        """强制重新加载"""
        with self._lock:
            self._load()
