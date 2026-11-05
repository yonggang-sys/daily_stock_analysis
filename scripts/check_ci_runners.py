#!/usr/bin/env python3
"""断言仓库内所有 GitHub Actions 的 ubuntu runner 都锁定到统一版本。

背景
----
本仓是 ZhuLinsen/daily_stock_analysis 的 fork，长期与上游双向流动：
  - 上游倾向 ``ubuntu-latest``（或为兼容性问题临时降到 ``ubuntu-24.04``）；
  - 本仓把 9 个 workflow 全量钉到 ``ubuntu-26.04``，避免 runner 镜像静默升级。

上游每次动 ``runs-on`` 都会与本仓在同一行冲突，导致 GitHub 网页「Sync fork」
整体拒绝（表现为「无法自动合并」）。``scripts/sync_upstream.sh`` 用
``git merge -X ours`` 把冲突块判给本仓，但 ``-X ours`` 不会覆盖「非冲突」的新增
job —— 上游若新加一个 ``ubuntu-latest`` 的 job，会静默漏网。本脚本就是那道兜底。

用法
----
    python scripts/check_ci_runners.py          # 校验，发现漂移则 exit 1
    python scripts/check_ci_runners.py --fix    # 就地改写为统一版本

可用 ``--expect`` 覆盖目标标签（默认 ubuntu-26.04）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DEFAULT_EXPECT = "ubuntu-26.04"

# runs-on: ubuntu-26.04  /  runs-on: ubuntu-latest   （标量形式）
_SCALAR_RE = re.compile(r"^(?P<indent>\s*runs-on:\s*)(?P<value>[^\s#]+)(?P<rest>.*)$")
# 列表形式的第一项：  - ubuntu-latest
_ITEM_RE = re.compile(r"^(?P<indent>\s*-\s*)(?P<value>ubuntu[-\w.]*)(?P<rest>.*)$")
_UBUNTU_RE = re.compile(r"^ubuntu(-[\w.]+)?$")

# 这些值不是 runner 标签本身，跳过
_SKIP_VALUES = {"self-hosted"}


def iter_workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    return [f for f in files if f.is_file()]


def _read_lines(path: Path) -> list[str]:
    """按原样读取行，保留每行自己的 EOL（LF / CRLF 都不动）。"""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.readlines()


def scan(path: Path, expect: str, fix: bool) -> tuple[list[str], bool]:
    """返回 (问题列表, 是否被改写)。"""
    problems: list[str] = []
    lines = _read_lines(path)
    changed = False

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        # 关键：正则只匹配「去掉行尾符后的正文」，改写时再把原行尾符拼回去。
        # 早期版本直接拿整行（含 EOL）去替换，rest 捕获不到换行符，导致 LF 文件
        # 被改写的行与下一行粘成一行（CRLF 文件因为 "\r" 被 .* 吃掉而侥幸没事）。
        body = line.rstrip("\r\n")
        eol = line[len(body):]

        for regex in (_SCALAR_RE, _ITEM_RE):
            m = regex.match(body)
            if not m:
                continue
            value = m.group("value")
            if value in _SKIP_VALUES or value.startswith("${{"):
                break
            if not _UBUNTU_RE.match(value):
                break
            if value == expect:
                break

            rel = path.relative_to(REPO_ROOT).as_posix()
            problems.append(f"{rel}:{idx}  {value}  ->  {expect}")
            if fix:
                lines[idx - 1] = m.group("indent") + expect + m.group("rest") + eol
                changed = True
            break

    if changed:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines))
    return problems, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fix", action="store_true", help="就地改写为统一 runner 标签")
    parser.add_argument("--expect", default=DEFAULT_EXPECT, help=f"期望的 runner 标签（默认 {DEFAULT_EXPECT}）")
    parser.add_argument("--quiet", action="store_true", help="仅在失败时输出")
    args = parser.parse_args()

    files = iter_workflow_files()
    if not files:
        print(f"[check_ci_runners] 未找到 workflow 文件：{WORKFLOW_DIR}", file=sys.stderr)
        return 2

    drift: list[str] = []
    fixed: list[str] = []
    for path in files:
        problems, changed = scan(path, args.expect, args.fix)
        drift.extend(problems)
        if changed:
            fixed.append(path.relative_to(REPO_ROOT).as_posix())

    if args.fix and fixed:
        print(f"[check_ci_runners] 已改写 {len(drift)} 处 -> {args.expect}")
        for p in drift:
            print(f"  fixed  {p}")
        return 0

    if drift:
        print(f"[check_ci_runners] 发现 {len(drift)} 处 runner 版本漂移（期望 {args.expect}）：", file=sys.stderr)
        for p in drift:
            print(f"  {p}", file=sys.stderr)
        print("\n修复：python scripts/check_ci_runners.py --fix", file=sys.stderr)
        return 1

    if not args.quiet:
        count = sum(
            1
            for f in files
            for line in f.read_text(encoding="utf-8").splitlines()
            if _SCALAR_RE.match(line) and _UBUNTU_RE.match(_SCALAR_RE.match(line).group("value"))
        )
        print(f"[check_ci_runners] OK — {len(files)} 个 workflow，{count} 个 ubuntu job 均为 {args.expect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
