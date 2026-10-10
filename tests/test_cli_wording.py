"""CLI 文案判据 —— spec §5.3 / §7.3 第一步的验收原文。

「`--help` 里不再出现"文档生成流水线"字样」：引擎是领域无关的工作流运行时
（docgen 只是装在上面的第一个 pack，spec §1），CLI 是它的门面，门面上不再
点名具体领域。

两条判据：
  1. 静态：run.py 源码零命中（钉 banner 与 argparse description 的所有写入点，
     比 subprocess 快且定位精确）；
  2. 动态：真跑 `--help`，stdout 用户可见面零命中。
"""
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

#: 要从 CLI 门面上清掉的字样（spec §5.3 验收判据原文）
FORBIDDEN = "文档生成流水线"


def test_run_py_source_has_no_docgen_wording():
    src = (PROJECT / "run.py").read_text(encoding="utf-8")
    assert FORBIDDEN not in src, (
        f"run.py 又出现「{FORBIDDEN}」：CLI 门面不得点名具体领域"
        "（docgen 只是 pack，spec §1/§5.3）")


def test_help_output_is_domain_free():
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "run.py"), "--help"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-500:]
    visible = proc.stdout + proc.stderr
    assert FORBIDDEN not in visible, (
        "--help 的用户可见输出里出现了「" + FORBIDDEN + "」")
