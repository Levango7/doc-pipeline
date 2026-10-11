"""LRUCache 与 load_env：搜索层的两件底座。

`cache.py` / `env.py` 是为迁出 search_engines 而新建的（原先它依赖消费方的
CacheManager 与 llm_router._load_env）。这里的判据盯的是**语义等价**：
TTL 三档（>0 到期 / ==0 永不过期 / <0 整块关闭）与 .env 的合并优先级，
都是迁出前那份实现里已经存在的行为，不能被"顺手重写"改掉。
"""
from __future__ import annotations

import inspect
import os
import re
import time
from pathlib import Path

import pytest

from artesian import search_engines as se
from artesian.cache import LRUCache
from artesian.env import load_env

# ──────────────────────────────── LRU 缓存 ────────────────────────────────

class TestLRUCacheBasics:
    def test_miss_returns_none(self):
        assert LRUCache().get("nope") is None

    def test_set_get_roundtrip(self):
        c = LRUCache()
        c.set("k", {"a": 1})
        assert c.get("k") == {"a": 1}

    def test_size_and_clear(self):
        c = LRUCache()
        c.set("a", 1)
        c.set("b", 2)
        assert c.size() == 2
        c.clear()
        assert c.size() == 0 and c.get("a") is None

    def test_remove_single_key(self):
        c = LRUCache()
        c.set("a", 1)
        c.remove("a")
        c.remove("不存在")          # 不报错
        assert c.size() == 0

    def test_overwrite_keeps_one_entry(self):
        c = LRUCache()
        c.set("a", 1)
        c.set("a", 2)
        assert c.get("a") == 2 and c.size() == 1

    def test_max_size_must_be_positive(self):
        with pytest.raises(ValueError):
            LRUCache(max_size=0)


class TestLRUOrder:
    def test_evicts_least_recently_used(self):
        c = LRUCache(max_size=2)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)               # 满了，逐出最久的 a
        assert c.get("a") is None
        assert c.get("b") == 2 and c.get("c") == 3

    def test_get_refreshes_recency(self):
        c = LRUCache(max_size=2)
        c.set("a", 1)
        c.set("b", 2)
        assert c.get("a") == 1      # a 变成最近使用
        c.set("c", 3)               # 该逐出的是 b
        assert c.get("b") is None
        assert c.get("a") == 1 and c.get("c") == 3


class TestTtlSemantics:
    def _aged(self, c: LRUCache, key: str, value, age: float):
        """把条目的写入时间往前推 age 秒（避免用例里 sleep）。"""
        c.set(key, value)
        c._data[key] = (value, time.time() - age)

    def test_positive_ttl_expires(self):
        c = LRUCache(ttl=60)
        self._aged(c, "k", "v", age=61)
        assert c.get("k") is None
        assert c.size() == 0                 # 过期即删，不留内存

    def test_positive_ttl_within_window_hits(self):
        c = LRUCache(ttl=60)
        self._aged(c, "k", "v", age=59)
        assert c.get("k") == "v"

    def test_zero_ttl_never_expires(self):
        """ttl=0 是"永不过期"，不是"立刻过期"——消费方按前者在用。"""
        c = LRUCache(ttl=0)
        self._aged(c, "k", "v", age=10 ** 6)
        assert c.get("k") == "v"

    def test_negative_ttl_disables_cache_entirely(self):
        """ttl<0 时读写皆空转：get 恒 None、set 不落条目、size 恒 0。"""
        c = LRUCache(ttl=-1)
        c.set("k", "v")
        assert c.get("k") is None
        assert c.size() == 0


# ──────────────────────────────── .env 读取 ────────────────────────────────

class TestLoadEnvFile:
    def test_reads_key_value_lines(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("A=1\nB=2\n", encoding="utf-8")
        env = load_env(str(f))
        assert env["A"] == "1" and env["B"] == "2"

    def test_skips_comments_blanks_and_lines_without_equals(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# 注释\n\n只有文字没有等号\nKEEP=yes\n", encoding="utf-8")
        env = load_env(str(f))
        assert env["KEEP"] == "yes"
        assert not any("注释" in k or "只有文字" in k for k in env)

    def test_strips_wrapping_quotes(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text('DQ="double"\nSQ=\'single\'\nBARE=plain\n', encoding="utf-8")
        env = load_env(str(f))
        assert env["DQ"] == "double" and env["SQ"] == "single"
        assert env["BARE"] == "plain"

    def test_empty_value_in_file_is_not_config(self, tmp_path, monkeypatch):
        """`KEY=` 不算配好了值；环境里也没有时该键就不该出现。"""
        monkeypatch.delenv("EMPTY_ONE", raising=False)
        f = tmp_path / ".env"
        f.write_text("EMPTY_ONE=\n", encoding="utf-8")
        assert "EMPTY_ONE" not in load_env(str(f))

    def test_system_value_wins_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOCHA_API_KEY", "from-system")
        f = tmp_path / ".env"
        f.write_text("BOCHA_API_KEY=from-file\n", encoding="utf-8")
        assert load_env(str(f))["BOCHA_API_KEY"] == "from-system"

    def test_empty_system_value_does_not_erase_file_value(self, tmp_path, monkeypatch):
        """系统里"存在但为空"不能把 .env 的有效值抹掉——这是最容易做错的一条。"""
        monkeypatch.setenv("BOCHA_API_KEY", "")
        f = tmp_path / ".env"
        f.write_text("BOCHA_API_KEY=sk-real\n", encoding="utf-8")
        assert load_env(str(f))["BOCHA_API_KEY"] == "sk-real"

    def test_missing_path_falls_back_to_os_environ(self, tmp_path):
        env = load_env(str(tmp_path / "不存在.env"))
        assert env == dict(os.environ)

    def test_unreadable_file_warns_but_still_returns_env(
            self, tmp_path, monkeypatch, caplog):
        """目录当文件传：open 抛 OSError，应当留痕而不是炸掉调用方。"""
        monkeypatch.setenv("SOLO", "v")
        with caplog.at_level("WARNING"):
            env = load_env(str(tmp_path))
        assert env["SOLO"] == "v"
        assert "加载 .env 失败" in caplog.text

    def test_none_searches_upwards_for_dot_env(self, tmp_path, monkeypatch):
        root = tmp_path / "proj"
        deep = root / "a" / "b"
        deep.mkdir(parents=True)
        (root / ".env").write_text("FOUND=1\n", encoding="utf-8")
        monkeypatch.chdir(deep)
        assert load_env()["FOUND"] == "1"

    def test_none_without_any_dot_env_uses_process_env(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ONLY_PROCESS", "yes")
        # 上溯可能命中仓库外的 .env，因此断言的是"至少包含进程环境"而不是相等
        assert load_env().get("ONLY_PROCESS") == "yes"


# ─────────────────────── ProSearch 脚本路径注入（迁出时的改动）───────────────────────

PROSEARCH_ENVS = ("PROSEARCH_PATH", "PROSEARCH_SCRIPT_PATH", "ARTESIAN_PROSEARCH_PATHS")


class TestProSearchPathResolution:
    """路径解析全部由宿主注入——库不假设任何具体机器。"""

    @pytest.fixture(autouse=True)
    def _clean_prosearch_env(self, monkeypatch):
        for k in PROSEARCH_ENVS:
            monkeypatch.delenv(k, raising=False)

    def test_no_paths_means_unavailable(self):
        """库里不再有隐式命中的默认路径：不给路径就是不可用。"""
        assert se.ProSearchEngine().is_available() is False

    def test_explicit_script_paths_wins(self, tmp_path):
        script = tmp_path / "p.cjs"
        script.write_text("// fake", encoding="utf-8")
        assert se.ProSearchEngine(script_paths=[str(script)])._script == str(script)

    def test_nonexistent_explicit_path_ignored(self, tmp_path):
        assert se.ProSearchEngine(
            script_paths=[str(tmp_path / "nope.cjs")]).is_available() is False

    def test_prosearch_path_env_single(self, tmp_path, monkeypatch):
        script = tmp_path / "env.cjs"
        script.write_text("// fake", encoding="utf-8")
        monkeypatch.setenv("PROSEARCH_PATH", str(script))
        assert se.ProSearchEngine()._script == str(script)

    def test_artesian_paths_env_list(self, tmp_path, monkeypatch):
        missing = tmp_path / "missing.cjs"
        found = tmp_path / "found.cjs"
        found.write_text("// fake", encoding="utf-8")
        monkeypatch.setenv("ARTESIAN_PROSEARCH_PATHS",
                           os.pathsep.join([str(missing), str(found)]))
        assert se.ProSearchEngine()._script == str(found)

    def test_script_path_env_overrides_earlier_match(self, tmp_path, monkeypatch):
        first = tmp_path / "first.cjs"
        winner = tmp_path / "winner.cjs"
        for p in (first, winner):
            p.write_text("// fake", encoding="utf-8")
        monkeypatch.setenv("PROSEARCH_PATH", str(first))
        monkeypatch.setenv("PROSEARCH_SCRIPT_PATH", str(winner))
        assert se.ProSearchEngine()._script == str(winner)

    def test_empty_env_value_is_not_treated_as_a_path(self, tmp_path, monkeypatch):
        """空串不当路径用：否则当前目录里一个同名文件就能被误认成脚本。"""
        monkeypatch.setenv("PROSEARCH_PATH", "")
        monkeypatch.chdir(tmp_path)
        assert se.ProSearchEngine().is_available() is False

    def test_module_carries_no_host_absolute_paths(self):
        """设计护栏：取数层不该内置某台机器上的盘符路径。

        正则要求"单独一个字母 + 冒号 + 斜杠"且前面是词边界，
        否则 `https://` 里的 `s:` 会被当成 Windows 路径误报。
        """
        src = Path(inspect.getfile(se)).read_text(encoding="utf-8")
        offenders = re.findall(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]{1,2}[^\s\"']*", src)
        assert not offenders, f"内置了本机路径，应改为显式注入：{offenders}"
