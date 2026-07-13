"""搜索结果缓存 - 24h TTL + 去重 + 断点续传"""

import os
import json
import time
import hashlib

CACHE_DIR = "scripts/cache"
CACHE_TTL = 24 * 3600


class ResultCache:
    """搜索结果缓存（文件级 JSON 缓存）"""

    def __init__(self, cache_dir: str = CACHE_DIR, ttl: int = CACHE_TTL):
        self.cache_dir = cache_dir
        self.ttl = ttl
        os.makedirs(cache_dir, exist_ok=True)

    def _key(self, query: str) -> str:
        return hashlib.sha256(query.encode()).hexdigest()

    def _path(self, query: str) -> str:
        return os.path.join(self.cache_dir, f"{self._key(query)}.json")

    def get(self, query: str):
        path = self._path(query)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)
            if time.time() - entry.get("ts", 0) > self.ttl:
                os.remove(path)
                return None
            return entry.get("data")
        except Exception:
            return None

    def set(self, query: str, data, source: str = "unknown"):
        path = self._path(query)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "query": query, "ts": time.time(),
                "source": source, "data": data
            }, f, ensure_ascii=False)

    def deduplicate(self, results: list) -> list:
        """基于 URL+标题指纹去重"""
        seen = set()
        out = []
        for r in results:
            sig = (r.get("url", "") + "|" + r.get("title", "")[:30])
            if sig not in seen:
                seen.add(sig)
                out.append(r)
        return out

    def clear(self):
        for f in os.listdir(self.cache_dir):
            if f.endswith(".json"):
                try:
                    os.remove(os.path.join(self.cache_dir, f))
                except Exception:
                    pass


class ResumeState:
    """断点续传状态管理"""

    def __init__(self, state_dir: str = "state"):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    def save(self, task_id: str, step: str, data):
        path = os.path.join(self.state_dir, f"{task_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"task_id": task_id, "step": step,
                       "ts": time.time(), "data": data}, f, ensure_ascii=False)

    def load(self, task_id: str):
        path = os.path.join(self.state_dir, f"{task_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def clear(self, task_id: str):
        path = os.path.join(self.state_dir, f"{task_id}.json")
        if os.path.exists(path):
            os.remove(path)
