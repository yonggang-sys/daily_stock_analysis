#!/usr/bin/env bash
# =============================================================================
# sync_fork_replay.sh
#
# 用途：在「Sync fork」上游之后，自动把本仓库对 00-daily-analysis.yml 的本地定制
#       （停用定时触发 schedule，仅保留手动 workflow_dispatch）重新应用到 main，
#       并提交、推回 origin，从而避免 GitHub「Sync fork」在 workflow 文件上冲突。
#
# 适用拓扑（本机现状）：
#   - 当前仓库 origin = yonggang-sys/daily_stock_analysis（即 GitHub 上带
#     「Sync fork」按钮的那个 fork/仓库，其上游 parent 在 GitHub 侧）。
#   - 用户在 GitHub 点「Sync fork」把 parent 合进 origin/main 后，origin/main
#     的 00-daily-analysis.yml 会变回「带 schedule 定时触发」；本脚本把本地
#     main（已 comment-out）与 origin/main 合并，自动化解冲突并重放本地改动。
#
# 用法（在仓库根目录执行）：
#   bash sync_fork_replay.sh                 # 完整：fetch -> merge -> 重放 -> 推送
#   NO_PUSH=1 bash sync_fork_replay.sh       # 不推送，仅本地合并+重放（先验证）
#   bash sync_fork_replay.sh --dry-run       # 仅打印重放差异，不写文件/不提交/不推送
#
# 可覆盖的环境变量：
#   REMOTE        上游/推送远程名（默认 origin）
#   BRANCH        目标分支（默认 main）
#   WORKFLOW      workflow 文件路径（默认 .github/workflows/00-daily-analysis.yml）
#   PY            Python 解释器（默认自动探测 python3 / python）
# =============================================================================
set -euo pipefail

REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
WORKFLOW="${WORKFLOW:-.github/workflows/00-daily-analysis.yml}"
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; fi
NO_PUSH="${NO_PUSH:-0}"

# --- 0. 预检 -----------------------------------------------------------------
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[error] 不在 git 仓库内，请在仓库根目录运行。" >&2
  exit 1
fi
if [ ! -f "$WORKFLOW" ]; then
  echo "[error] 找不到 workflow 文件: $WORKFLOW" >&2
  exit 1
fi

# Python 解释器探测
if [ -z "${PY:-}" ]; then
  PY="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PY" ]; then
  echo "[error] 未找到 python3/python，请设置 PY 环境变量。" >&2
  exit 1
fi

# 定位本脚本同目录的 replay 工具（pwd -W 给出 Windows 原生路径，避免 Git Bash 下被 python 二次转换）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -W 2>/dev/null)"
if [ -z "$SCRIPT_DIR" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
fi
REPLAY="$SCRIPT_DIR/replay_workflow_edit.py"
if [ ! -f "$REPLAY" ]; then
  echo "[error] 找不到 replay_workflow_edit.py（应与本脚本同目录）: $REPLAY" >&2
  exit 1
fi

echo "==> 远程=$REMOTE 分支=$BRANCH 文件=$WORKFLOW"

# --- 1. 取最新 ---------------------------------------------------------------
git fetch "$REMOTE"

# --- 2. 合并上游（--no-ff 保留 fork 独有历史；冲突时自动重放）----------------
MERGE_MSG="chore: sync ${REMOTE}/${BRANCH} and replay local workflow customization (disable schedule)"
if ! git merge --no-ff -m "$MERGE_MSG" "${REMOTE}/${BRANCH}"; then
  echo "[info] 合并产生冲突，尝试自动化解 $WORKFLOW ..."
  if git diff --name-only --diff-filter=U | grep -qx "$WORKFLOW"; then
    # 以远端合并后的版本为基底，再重放本地 comment-out
    git checkout "${REMOTE}/${BRANCH}" -- "$WORKFLOW"
    if [ "$DRY_RUN" -eq 0 ]; then
      "$PY" "$REPLAY" "$WORKFLOW"
      git add "$WORKFLOW"
    else
      "$PY" "$REPLAY" "$WORKFLOW" --dry-run
    fi
    # 仅剩 workflow 冲突时应可完成合并；若仍有其它冲突则报错交人工
    if [ "$(git diff --name-only --diff-filter=U | wc -l | tr -d ' ')" = "0" ]; then
      if [ "$DRY_RUN" -eq 0 ]; then
        git commit --no-edit
        echo "[ok] 合并冲突已用本地定制解决。"
      else
        echo "[dry-run] 跳过提交（--dry-run）。"
      fi
    else
      echo "[error] 除 $WORKFLOW 外还有未解决冲突，请人工处理: git status" >&2
      exit 1
    fi
  else
    echo "[error] 合并冲突不在预期文件，请人工处理: git status" >&2
    exit 1
  fi
fi

# --- 3. 幂等重放（确保本地定制始终就位）-------------------------------------
if [ "$DRY_RUN" -eq 0 ]; then
  "$PY" "$REPLAY" "$WORKFLOW"
  if ! git diff --quiet "$WORKFLOW"; then
    git add "$WORKFLOW"
    git commit -m "chore: replay 'disable schedule, keep manual trigger' on $WORKFLOW"
    echo "[ok] 已提交重放改动。"
  fi
else
  echo "[dry-run] 重放预览："
  "$PY" "$REPLAY" "$WORKFLOW" --dry-run
fi

# --- 4. 推送 -----------------------------------------------------------------
if [ "$NO_PUSH" -eq 1 ]; then
  echo "[skip] NO_PUSH=1，未推送。如需推送：git push $REMOTE $BRANCH"
elif [ "$DRY_RUN" -eq 1 ]; then
  echo "[skip] --dry-run，未推送。"
else
  git push "$REMOTE" "$BRANCH"
  echo "[ok] 已推送到 $REMOTE/$BRANCH。"
fi

echo "==> 完成。"
