"""CLI 输出通道契约：stdout 只放机器要读的东西，人类可读的过程输出去 stderr。

实测过两种污染：`--mcp` 的启动 banner 排在 JSON-RPC 帧之前（FP-3，已修）；
`--json-output` 的 stdout 上有 11 行 `[run] …` 过程输出，wrapper 逐行解析必然炸。
两条都属于同一类缺陷：**被承诺为机器通道的地方混进了人类输出**。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# 离线可跑的最小工作流：transform 渲染一次 → safe_writer 落盘，不碰网络也不碰 LLM
_OFFLINE_YAML = """\
_name: probe-cli-channels
_version: "1.0"
description: 输出通道探针（无网络、无 LLM）

agents:
  - name: transform
    version: "1.0"
    dependencies: []
    config:
      items: ""
      template: "channel probe ok\\n"

  - name: safe_writer
    version: "2.0"
    dependencies: ["transform"]
    config:
      backup_dir: backups
      atomic: true

topology:
  type: dag
  levels:
    - [transform]
    - [safe_writer]
  edges:
    - [transform, safe_writer]

pipeline:
  timeout: 120
  fail_fast: false
"""


def _run(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 状态目录隔离：幂等库按检出绝对路径共享，跑在仓库 bus_data/ 上会与真实运行串台
    env["DOC_PIPELINE_STATE_DIR"] = str(tmp_path / "state")
    env["DOC_PIPELINE_VERSIONS_DIR"] = str(tmp_path / "versions")
    return subprocess.run([sys.executable, "run.py", *args],
                          capture_output=True, text=True, timeout=180,
                          cwd=str(ROOT), env=env)


@pytest.fixture
def offline_plan(tmp_path) -> str:
    plan = tmp_path / "probe-cli-channels.yaml"
    plan.write_text(_OFFLINE_YAML, encoding="utf-8")
    return str(plan)


class TestCLIOutputChannels:
    def test_json_output_is_the_only_stdout_line(self, offline_plan, tmp_path):
        out_file = tmp_path / "artifact.md"
        proc = _run(["test_input.md", "--pipeline-file", offline_plan,
                     "-o", str(out_file), "--json-output"], tmp_path)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, (
            f"--json-output 的 stdout 必须是单行 JSON，实得 {len(lines)} 行：{lines[:3]}")
        payload = json.loads(lines[0])
        assert payload["status"] == "done", payload
        assert payload["exit_code"] == 0, payload
        assert out_file.exists(), "声明了落盘节点就必须有交付物"

    def test_human_readable_progress_goes_to_stderr(self, offline_plan, tmp_path):
        proc = _run(["test_input.md", "--pipeline-file", offline_plan,
                     "-o", str(tmp_path / "a.md"), "--json-output"], tmp_path)
        # 过程输出仍在，只是换了通道——不许静默丢弃
        assert "[run]" in proc.stderr or "流水线" in proc.stderr, \
            f"人类可读输出被弄丢了：stderr={proc.stderr[:300]!r}"

    def test_plain_mode_still_prints_human_report_on_stdout(self, offline_plan, tmp_path):
        """不带 --json-output 时不得反向回归：人看的报告必须在 stdout。"""
        proc = _run(["test_input.md", "--pipeline-file", offline_plan,
                     "-o", str(tmp_path / "b.md")], tmp_path)
        assert "流水线执行完成" in proc.stdout or "执行步骤" in proc.stdout, \
            f"默认模式的 stdout 报告不见了：rc={proc.returncode} stderr={proc.stderr[-300:]}"
        # banner 属于"给人看的开场白"，FP-3 之后一律 stderr（stdout 留给交付物通道）
        assert "Doc-Pipeline" in proc.stderr, "banner 应从 stderr 可见，不许被静默丢弃"
        assert "Doc-Pipeline" not in proc.stdout
