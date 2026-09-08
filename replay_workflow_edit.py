#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replay_workflow_edit.py — 幂重放「停用定时触发」的本地改动到 GitHub Actions 工作流文件。

作用：
  在 YAML 的 `on:` 块内，将 `schedule:` 及其嵌套的 `- cron:` 行注释掉，
  并确保 `workflow_dispatch:` 保持「未注释」（手动触发始终可用）。
  该操作是幂等的：若 `schedule:` 已被注释，则不做任何改动。

用法：
  python3 replay_workflow_edit.py <workflow.yml> [--dry-run]

依赖：仅标准库。
"""
import sys
import re


def replay(path, dry_run=False):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    out = []
    in_on = False
    in_schedule = False
    changed = False

    # 匹配顶层的 on: 块起始（缩进为 0 且为 on:）
    re_on = re.compile(r"^on:\s*$")
    # on: 块内的子键（典型缩进 2）；workflow_dispatch 允许已被上游注释的形式
    re_schedule = re.compile(r"^schedule:\s*$")
    re_dispatch = re.compile(r"^#?\s*workflow_dispatch:\s*$")
    # 任意顶层键（缩进 0 且以字母开头）表示 on: 块结束
    re_topkey = re.compile(r"^[A-Za-z_]")

    for line in lines:
        raw = line.rstrip("\n")
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)

        if indent == 0 and re_on.match(stripped):
            in_on = True
            in_schedule = False
            out.append(line)
            continue

        if in_on:
            # 遇到下一个顶层键 => on: 块结束
            if indent == 0 and re_topkey.match(stripped):
                in_on = False
                in_schedule = False
                out.append(line)
                continue

            # 命中 schedule: 键 -> 进入注释区
            if re_schedule.match(stripped):
                in_schedule = True
                if not stripped.startswith("#"):
                    raw = (" " * indent) + "# " + stripped
                    changed = True
                out.append(raw + "\n")
                continue

            # 命中 workflow_dispatch: 键 -> 退出注释区，并确保未注释（保留手动触发）
            if re_dispatch.match(stripped):
                in_schedule = False
                body = re.sub(r"^#\s*", "", stripped)
                if body != stripped:
                    raw = (" " * indent) + body
                    changed = True
                out.append(raw + "\n")
                continue

            # 处于 schedule 注释区内且是嵌套子行（cron 等）-> 一并注释
            if in_schedule and indent > 0:
                if raw.strip() == "":
                    out.append(line)
                    continue
                if not stripped.startswith("#"):
                    raw = (" " * indent) + "# " + stripped
                    changed = True
                out.append(raw + "\n")
                continue

        out.append(line)

    if changed and not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(out)

    return changed


def main():
    if len(sys.argv) < 2:
        print("用法: python3 replay_workflow_edit.py <workflow.yml> [--dry-run]", file=sys.stderr)
        sys.exit(2)

    path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv[2:]

    try:
        changed = replay(path, dry_run=dry_run)
    except FileNotFoundError:
        print(f"[error] 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)

    if changed:
        print(f"[{'dry-run ' if dry_run else ''}ok] 已重放 comment-out schedule: {path}")
    else:
        print(f"[ok] 无需改动（已是停用定时触发状态）: {path}")


if __name__ == "__main__":
    main()
