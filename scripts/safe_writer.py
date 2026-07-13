"""
SafeWriter v4 - 安全写入工具（改进版）
=========================================
改进点 v4：
  - manifest 校验和（防损坏）+ 自动从 .bak 恢复
  - 分级备份保留策略（30天全保/90天保最新/之后删除）
  - 写入前自动显示 diff 摘要
  - 验证规则：大小比、行数比
  - 支持 content-file 参数（从文件读取内容）
  - 支持 --newline 参数（lf/crlf/auto）
"""

import os
import sys
import json
import shutil
import hashlib
import argparse
import difflib
import tempfile
import datetime
from pathlib import Path


MANIFEST_FILE = "manifest.json"
TIER_FULL = 30      # 0-30天：全部保留
TIER_LATEST = 90    # 30-90天：只保留最新版本
                    # 90天后：全部删除


# =============================================================================
# 工具函数
# =============================================================================

def now_ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def file_info(path: str) -> dict:
    """读取文件基本信息"""
    if not os.path.exists(path):
        return {"exists": False, "size": 0, "lines": 0, "sha256": ""}
    with open(path, "rb") as f:
        raw = f.read()
    return {
        "exists": True,
        "size": len(raw),
        "lines": raw.count(b"\n") + 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def file_checksum(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# =============================================================================
# Manifest 管理
# =============================================================================

def load_manifest(path: str) -> dict:
    """加载 manifest，自动校验和修复"""
    if not os.path.exists(path):
        return {"version": "4.0", "files": {}, "checksum": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 校验和验证
        stored = data.get("checksum")
        if stored:
            actual = file_checksum(path)
            if actual != stored:
                print(f"[SafeWriter] ⚠  manifest 校验和不匹配，尝试从备份恢复...")
                data = _restore_manifest(path, data)

        return data
    except Exception:
        return {"version": "4.0", "files": {}, "checksum": None}


def save_manifest(path: str, data: dict, backup: bool = True):
    """保存 manifest（自动生成校验和）"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    # 先备份旧 manifest
    if backup and os.path.exists(path):
        shutil.copy2(path, path + ".bak")

    # 清空旧 checksum 再写入
    data.pop("checksum", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 计算并写入 checksum
    chk = file_checksum(path)
    data["checksum"] = chk
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _restore_manifest(path: str, corrupted: dict) -> dict:
    """从 .bak 恢复 manifest"""
    bak = path + ".bak"
    if os.path.exists(bak):
        try:
            with open(bak, "r", encoding="utf-8") as f:
                restored = json.load(f)
            print(f"[SafeWriter] 已从 manifest.bak 恢复")
            return restored
        except Exception:
            pass
    print(f"[SafeWriter] 无法恢复 manifest，使用空值")
    return {"version": "4.0", "files": {}, "checksum": None}


# =============================================================================
# Diff 预览
# =============================================================================

def diff_preview(old: str, new: str, context: int = 3,
                 max_lines: int = 60) -> str:
    """生成 unified diff 预览"""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="当前版本", tofile="新版本", n=context
    ))

    if not diff:
        return "(无差异)"

    adds = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    summary = f"[+{adds} 行 / -{dels} 行]"

    if len(diff) > max_lines:
        half = max_lines // 2
        diff = diff[:half] + ["...(省略中间差异)...\n"] + diff[-half:]

    return summary + "\n" + "".join(diff)


# =============================================================================
# 分级清理
# =============================================================================

def cleanup_tiered(backup_dir: str,
                   tier_full: int = TIER_FULL,
                   tier_latest: int = TIER_LATEST) -> dict:
    """
    分级清理备份文件

    - 0~tier_full 天：全部保留
    - tier_full~tier_latest 天：只保留最新版本
    - tier_latest 天以上：全部删除
    """
    import time

    backup_dir = str(Path(backup_dir).resolve())
    manifest_path = os.path.join(backup_dir, MANIFEST_FILE)

    if not os.path.exists(manifest_path):
        return {"deleted": 0, "tiered": True}

    man = load_manifest(manifest_path)
    now = time.time()
    t_full = tier_full * 86400
    t_latest = tier_latest * 86400
    deleted = []

    for rel, finfo in man.get("files", {}).items():
        backups = finfo.get("backups", [])
        if not backups:
            continue

        def parse_ts(b):
            try:
                return datetime.datetime.fromisoformat(b["timestamp"]).timestamp()
            except Exception:
                return 0

        sorted_backups = sorted(backups, key=parse_ts, reverse=True)
        latest_ts = parse_ts(sorted_backups[0]) if sorted_backups else 0
        to_delete = []

        for b in sorted_backups:
            age = now - parse_ts(b)
            bpath = b.get("path", "")
            if age < t_full:
                pass  # 保留
            elif age < t_latest:
                if parse_ts(b) != latest_ts:
                    to_delete.append(bpath)
            else:
                to_delete.append(bpath)

        for bpath in to_delete:
            if bpath and os.path.exists(bpath):
                try:
                    os.remove(bpath)
                    deleted.append(bpath)
                except Exception as e:
                    print(f"[SafeWriter] 删除失败 {bpath}: {e}")

        finfo["backups"] = [
            b for b in sorted_backups
            if b.get("path") not in to_delete
        ]
        if finfo["backups"]:
            finfo["latest"] = finfo["backups"][0]
        else:
            finfo["latest"] = None

    if deleted:
        save_manifest(manifest_path, man)
        print(f"[SafeWriter] 分级清理: 删除 {len(deleted)} 个过期备份")

    return {"deleted": len(deleted), "tiered": True, "files": deleted}


# 向后兼容
cleanup_old_backups = cleanup_tiered


# =============================================================================
# 核心写入
# =============================================================================

def safe_write(target: str, content: str,
               backup_dir: str = "backups",
               reason: str = "auto",
               agent: str = "SafeWriter",
               dry_run: bool = False,
               show_diff: bool = True,
               newline: str = "auto") -> dict:
    """
    安全写入

    流程：
      1. 读取现有文件信息
      2. 显示 diff（可选）
      3. 备份原文件 + 更新 manifest
      4. 写入临时文件
      5. 验证临时文件
      6. 原子替换
      7. 分级清理过期备份

    参数：
      target:     目标文件路径
      content:    新内容（str）
      backup_dir: 备份目录
      reason:     本次修改原因（记录到 manifest）
      agent:      执行者名称（记录到 manifest）
      dry_run:    True = 仅验证，不写入
      show_diff:  是否显示 diff 预览
      newline:    "auto"（保持原有）/ "lf" / "crlf"
    """
    target = str(Path(target).resolve())
    backup_dir = str(Path(backup_dir).resolve())
    manifest_path = os.path.join(backup_dir, MANIFEST_FILE)

    info = file_info(target)
    backup_path = None

    print(f"\n[SafeWriter] 目标: {target}")
    print(f"[SafeWriter] 原因: {reason}")

    # Step 1: diff 预览
    if info["exists"] and show_diff and not dry_run:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            old_content = f.read()
        diff = diff_preview(old_content, content)
        print(f"[SafeWriter] 变更预览:\n{'─'*50}")
        for ln in diff.splitlines()[:40]:
            print(f"  {ln}")
        if len(diff.splitlines()) > 40:
            print("  ...(更多差异省略)...")
        print("─" * 50)

    # Step 2: 备份
    if info["exists"]:
        backup_path = os.path.join(
            backup_dir,
            f"{Path(target).stem}_{now_ts()}{Path(target).suffix}"
        )
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(target, backup_path)

        # 更新 manifest
        man = load_manifest(manifest_path)
        if target not in man["files"]:
            man["files"][target] = {"backups": [], "latest": None}
        entry = {
            "path": str(Path(backup_path).resolve()),
            "timestamp": datetime.datetime.now().isoformat(),
            "size": info["size"],
            "lines": info["lines"],
            "sha256": info["sha256"],
            "reason": reason,
            "agent": agent,
        }
        man["files"][target]["backups"].append(entry)
        man["files"][target]["latest"] = entry
        save_manifest(manifest_path, man)

        print(f"[SafeWriter] ✓ 备份: {Path(backup_path).name} ({info['size']:,} bytes, {info['lines']} 行)")

    # Step 3: 换行符处理
    if newline == "crlf":
        content = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    elif newline == "lf":
        content = content.replace("\r\n", "\n").replace("\r", "\n")
    # "auto" = 保持传入 content 的换行符

    # Step 4: 写入临时文件
    ext = Path(target).suffix.lower()
    enc = "utf-8-sig" if ext in {".csv", ".tsv"} else "utf-8"
    tmp_dir = str(Path(target).parent)

    try:
        fd, tmp_path = tempfile.mkstemp(
            suffix=Path(target).suffix, prefix=".tmp_", dir=tmp_dir
        )
        os.close(fd)
        content_bytes = content.encode(enc)
        with open(tmp_path, "wb") as f:
            f.write(content_bytes)
        new_size = len(content_bytes)
        new_lines = content.count("\n") + 1
        print(f"[SafeWriter] ✓ 临时文件: {new_size:,} bytes, {new_lines} 行")
    except Exception as e:
        return {"status": "error", "message": f"写入临时文件失败: {e}"}

    # Step 5: 验证
    issues = []
    if not content.strip():
        issues.append("P0: 内容为空")
    elif info["exists"] and info["size"] > 0:
        size_ratio = new_size / info["size"]
        if size_ratio < 0.5:
            issues.append(f"P1: 文件缩减超50% ({info['size']:,} → {new_size:,} bytes)")
        elif size_ratio > 10:
            issues.append(f"P1: 文件增大超10倍 ({info['size']:,} → {new_size:,} bytes)")
        line_ratio = new_lines / max(info["lines"], 1)
        if line_ratio < 0.7:
            issues.append(f"P1: 行数减少超30% ({info['lines']} → {new_lines} 行)")

    if issues:
        os.unlink(tmp_path)
        print(f"[SafeWriter] ✗ 验证失败:")
        for iss in issues:
            print(f"    {iss}")
        return {"status": "error", "issues": issues, "backup": backup_path}

    print(f"[SafeWriter] ✓ 验证通过")

    if dry_run:
        if info["exists"]:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                diff_txt = diff_preview(f.read(), content)
        else:
            diff_txt = None
        os.unlink(tmp_path)
        return {
            "status": "ok", "dry_run": True,
            "backup": backup_path, "diff_preview": diff_txt
        }

    # Step 6: 原子替换
    try:
        if os.path.exists(target):
            os.remove(target)
        os.rename(tmp_path, target)
    except Exception as e:
        print(f"[SafeWriter] ✗ 原子替换失败: {e}")
        return {"status": "error", "message": str(e)}

    # Step 7: 分级清理
    cleanup_tiered(backup_dir)

    print(f"[SafeWriter] ✅ 写入成功: {target} ({new_size:,} bytes, {new_lines} 行)\n")

    return {
        "status": "ok",
        "target": target,
        "backup": backup_path,
        "size": new_size,
        "lines": new_lines,
    }


# =============================================================================
# CLI
# =============================================================================

def main():
    p = argparse.ArgumentParser(description="SafeWriter v4 - 安全写入工具")
    p.add_argument("--target", "-t", help="目标文件路径（必填，或使用 --cleanup）")
    p.add_argument("--content", "-c", default="", help="新内容（字符串）")
    p.add_argument("--content-file", "-f", help="从文件读取新内容")
    p.add_argument("--backup-dir", "-b", default="backups", help="备份目录")
    p.add_argument("--reason", "-r", default="cli", help="修改原因")
    p.add_argument("--agent", default="CLI", help="执行者")
    p.add_argument("--dry-run", "-n", action="store_true", help="仅验证，不写入")
    p.add_argument("--no-diff", action="store_true", help="禁用 diff 预览")
    p.add_argument("--newline", choices=["auto", "lf", "crlf"], default="auto",
                   help="换行符风格")
    p.add_argument("--cleanup", action="store_true", help="仅执行分级清理")
    p.add_argument("--tier-full", type=int, default=TIER_FULL,
                   help=f"全量保留天数（默认 {TIER_FULL}）")
    p.add_argument("--tier-latest", type=int, default=TIER_LATEST,
                   help=f"保留最新天数（默认 {TIER_LATEST}）")
    args = p.parse_args()

    if args.cleanup:
        if not args.target:
            print("--cleanup 需要 --target 指定备份目录或文件")
            return
        backup_dir = args.backup_dir
        result = cleanup_tiered(backup_dir, args.tier_full, args.tier_latest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if not args.target:
        print("错误: --target 是必填参数（或使用 --cleanup）")
        sys.exit(1)

    if args.content_file:
        with open(args.content_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    else:
        content = args.content

    result = safe_write(
        target=args.target,
        content=content,
        backup_dir=args.backup_dir,
        reason=args.reason,
        agent=args.agent,
        dry_run=args.dry_run,
        show_diff=not args.no_diff,
        newline=args.newline,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] in {"ok"} else 1)


if __name__ == "__main__":
    main()
