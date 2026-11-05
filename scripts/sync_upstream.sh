#!/usr/bin/env bash
# 与上游同步，确定性地解决 runner 版本冲突。
#
# 为什么需要这个脚本
# ------------------
# 本仓把 20 个 ubuntu job 全量钉在 ubuntu-26.04，上游倾向 ubuntu-latest / ubuntu-24.04。
# 双方改的是同一行 `runs-on:`，Git 必然冲突；而 GitHub 网页的「Sync fork」遇到
# **任何**冲突就整体拒绝，表现成「无法自动合并」（哪怕只冲突一行）。
#
# 解法：不用网页按钮，改用本脚本。对「本仓只改过 runner 的文件」，直接取上游
# 完整版本（上游改动一字不丢），再用 check_ci_runners.py 把 runner 统一回 26.04。
#
# 为什么不用 `git merge -X ours`
# ------------------------------
# 实测（/tmp/mt3）：`-X ours` 会把冲突 hunk 邻近的上游行改动一起判给本仓。
# 例：上游同时改了 `runs-on` 和紧邻的 `OTHER: v1 -> v2`，-X ours 后 OTHER 退回 v1，
# 上游修复被静默丢弃。本脚本的 --theirs + 回注没有这个问题（已实测 OTHER 保留 v2）。
#
# 用法
# ----
#   bash scripts/sync_upstream.sh            # 只合并，不推送
#   PUSH=1 bash scripts/sync_upstream.sh     # 合并 + 推送 origin
#
# 环境变量
#   UPSTREAM_REMOTE  默认 upstream
#   UPSTREAM_BRANCH  默认 main
#   ORIGIN_REMOTE    默认 origin
#   PUSH=1           合并后推送
#   PYTHON           指定解释器（默认 python3）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"
ORIGIN_REMOTE="${ORIGIN_REMOTE:-origin}"
PUSH="${PUSH:-0}"
PYTHON="${PYTHON:-python3}"

UP_REF="${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!  %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# 该路径在本仓相对 merge-base 的改动，是否只有 runs-on 行。
is_runner_only() {
    local file="$1"
    local mb nonrunner
    mb="$(git merge-base HEAD "$UP_REF")"
    nonrunner="$(git diff "$mb"..HEAD -- "$file" 2>/dev/null \
        | grep '^[+-]' | grep -v '^+++' | grep -v '^---' | grep -v 'runs-on:' | wc -l | tr -d ' ')"
    [ "$nonrunner" = "0" ]
}

# ---------- 0. 前置检查 ----------
say "0/6 前置检查"
[ -d .git ] || die "当前目录不是 git 仓库：$REPO_ROOT"
[ -e .git/MERGE_HEAD ] && die "存在未完成的合并（.git/MERGE_HEAD）。先 git merge --abort 或完成它。"
if [ -n "$(git status --porcelain)" ]; then
    git status --short
    die "工作区不干净，先提交或 stash。"
fi
git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1 || die "远程 $UPSTREAM_REMOTE 不存在。先执行：
  git remote add $UPSTREAM_REMOTE https://github.com/ZhuLinsen/daily_stock_analysis.git"
echo "  upstream = $(git remote get-url "$UPSTREAM_REMOTE" | sed 's#//[^@]*@#//***@#')"

# ---------- 1. 拉取上游 ----------
say "1/6 拉取 $UP_REF"
git fetch "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH"
BEHIND=$(git rev-list --count "HEAD..${UP_REF}")
if [ "$BEHIND" -eq 0 ]; then
    echo "  已与上游一致，无需合并。"
    exit 0
fi
echo "  上游领先 $BEHIND 个提交"

# ---------- 2. 合并 ----------
say "2/6 合并"
set +e
git merge --no-commit --no-ff "$UP_REF" >/tmp/sync_merge.log 2>&1
MERGE_RC=$?
set -e

CONFLICTS="$(git diff --name-only --diff-filter=U || true)"
if [ "$MERGE_RC" -eq 0 ] && [ -z "$CONFLICTS" ]; then
    echo "  零冲突，直接提交。"
    git commit --no-edit --quiet
    STRATEGY="clean"
else
    echo "  冲突文件："
    printf '    %s\n' $CONFLICTS
    ALL_OK=1
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        case "$f" in
            .github/workflows/*.yml|.github/workflows/*.yaml)
                if is_runner_only "$f"; then
                    git checkout --theirs -- "$f"
                    git add -- "$f"
                    echo "  ✓ $f 本仓只改过 runner → 取上游完整版本，稍后回注 26.04"
                else
                    warn "$f 含本仓自定义改动（非 runner），不能简单取上游"
                    ALL_OK=0
                fi
                ;;
            *)
                warn "$f 不在 .github/workflows/，可能涉及业务逻辑"
                ALL_OK=0
                ;;
        esac
    done <<< "$CONFLICTS"

    if [ "$ALL_OK" -ne 1 ]; then
        git merge --abort
        die "已 abort，仓库未改动。请手工 git merge $UP_REF 处理上面标记的文件。"
    fi

    say "2b/6 回注统一 runner"
    "$PYTHON" scripts/check_ci_runners.py --fix
    git add .github/workflows
    git commit --no-edit --quiet
    STRATEGY="theirs + 回注"
fi
echo "  合并方式：$STRATEGY"

# ---------- 3. runner 版本兜底（含上游新增的 job） ----------
say "3/6 统一 runner 版本"
"$PYTHON" scripts/check_ci_runners.py --fix
if [ -n "$(git status --porcelain)" ]; then
    git add .github/workflows
    git commit --quiet -m "ci: 同步上游后统一 runner 至 ubuntu-26.04

由 scripts/sync_upstream.sh 自动追加：上游新增/修改的 job 若使用
ubuntu-latest 或 ubuntu-24.04，在此统一纠正，避免与 26.04 锁定策略漂移。"
    echo "  已追加一条 runner 对齐提交。"
else
    echo "  无需改写。"
fi

# ---------- 4. 断言本仓自定义补丁仍在 ----------
say "4/6 断言自定义补丁"
FAIL=0
assert_grep() {
    local file="$1" pattern="$2" label="$3"
    if grep -q -- "$pattern" "$file" 2>/dev/null; then
        printf '  ✓ %s\n' "$label"
    else
        printf '  ✗ %s（%s 中未找到 %s）\n' "$label" "$file" "$pattern"
        FAIL=1
    fi
}

assert_grep .github/workflows/00-daily-analysis.yml "AI_TARGETS_USED" "自选股来源走 config/ai_targets.json"
assert_grep src/analyzer.py "mark_llm_model_unhealthy" "LLM 模型熔断"
assert_grep src/analyzer.py "llm_model_cooldown_remaining" "熔断冷却查询"
assert_grep src/notification.py "_brief_failure_reason" "摘要行失败原因"

if [ -f config/ai_targets.json ]; then
    N=$("$PYTHON" -c "import json;print(len(json.load(open('config/ai_targets.json',encoding='utf-8')).get('targets') or []))" 2>/dev/null || echo 0)
    if [ "$N" -gt 0 ]; then
        printf '  ✓ ai_targets.json 有 %s 只标的\n' "$N"
    else
        printf '  ✗ ai_targets.json targets 为空\n'; FAIL=1
    fi
else
    printf '  ✗ config/ai_targets.json 缺失\n'; FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
    die "自定义补丁校验未通过，请人工复核后再推送。合并结果已保留在工作区。"
fi

# ---------- 5. 语法与结构校验 ----------
say "5/6 校验 workflow 与 Python 语法"
"$PYTHON" - <<'PY'
import sys
try:
    import yaml
except ImportError:
    print("  (跳过 YAML 校验：未安装 pyyaml)")
    sys.exit(0)
from pathlib import Path
bad = []
for p in sorted(Path(".github/workflows").glob("*.yml")) + sorted(Path(".github/workflows").glob("*.yaml")):
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        jobs = (d or {}).get("jobs") or {}
        print(f"  ✓ {p.name}: {len(jobs)} 个 job")
    except Exception as e:
        bad.append(f"{p.name}: {e}")
for b in bad:
    print(f"  ✗ {b}")
sys.exit(1 if bad else 0)
PY

"$PYTHON" -m py_compile main.py ci_emit.py src/analyzer.py src/config.py src/notification.py
echo "  ✓ py_compile 通过"

# ---------- 6. 推送 ----------
say "6/6 结果"
git log --oneline -3
if [ "$PUSH" = "1" ]; then
    echo "  推送 $ORIGIN_REMOTE ..."
    git push "$ORIGIN_REMOTE" HEAD
else
    echo "  未推送。确认后执行：git push $ORIGIN_REMOTE HEAD"
fi
