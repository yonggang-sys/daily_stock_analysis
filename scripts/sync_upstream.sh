#!/usr/bin/env bash
# 与上游同步，自动化解 runner 版本冲突。
#
# 为什么需要这个脚本
# ------------------
# 本仓把 9 个 workflow 全量钉在 ubuntu-26.04，上游倾向 ubuntu-latest / ubuntu-24.04。
# 双方改的是同一行 `runs-on:`，Git 必然冲突；而 GitHub 网页的「Sync fork」遇到
# **任何**冲突就整体拒绝，表现成「无法自动合并」（哪怕只冲突一行）。
#
# 本脚本绕开网页按钮，流程：
#   1. 探针合并，摸清冲突面
#   2. 冲突只在 .github/workflows/ → 用 `git merge -X ours` 把冲突块判给本仓
#      （非冲突块照常吸收上游改动，不会丢上游修复）
#   3. check_ci_runners.py --fix 兜底：连上游新增的 ubuntu-latest job 也纠正为 26.04
#   4. 断言本仓自定义补丁仍在（AI 自选股注入、模型熔断、失败原因摘要）
#   5. 可选推送
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
#   PYTHON           指定解释器

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
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

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

# ---------- 2. 探针合并：摸清冲突面 ----------
say "2/6 探针合并（不落地）"
set +e
git merge --no-commit --no-ff "$UP_REF" >/tmp/sync_probe.log 2>&1
PROBE_RC=$?
set -e

CONFLICTS="$(git diff --name-only --diff-filter=U || true)"
if [ "$PROBE_RC" -eq 0 ] && [ -z "$CONFLICTS" ]; then
    echo "  无冲突，直接提交合并。"
    git commit --no-edit --quiet
    MERGED_STRATEGY="clean"
else
    echo "  冲突文件："
    printf '    %s\n' $CONFLICTS
    NON_WORKFLOW="$(printf '%s\n' "$CONFLICTS" | grep -v '^\.github/workflows/' || true)"
    git merge --abort
    if [ -n "$NON_WORKFLOW" ]; then
        printf '\n\033[31m✗ 冲突不只在 workflow，需人工判断：\033[0m\n'
        printf '    %s\n' $NON_WORKFLOW
        die "已 abort，仓库未改动。请手工 git merge $UP_REF 处理上述文件。"
    fi
    say "2b/6 冲突仅在 workflow，用 -X ours 重合并（冲突块判给本仓）"
    git merge -X ours --no-edit "$UP_REF"
    MERGED_STRATEGY="-X ours"
fi
echo "  合并方式：$MERGED_STRATEGY"

# ---------- 3. runner 版本兜底 ----------
say "3/6 统一 runner 版本"
if "$PYTHON" scripts/check_ci_runners.py --fix; then
    if [ -n "$(git status --porcelain)" ]; then
        git add .github/workflows
        git commit --quiet -m "ci: 同步上游后统一 runner 至 ubuntu-26.04

由 scripts/sync_upstream.sh 自动追加：上游新增/修改的 job 若使用
ubuntu-latest 或 ubuntu-24.04，在此统一纠正，避免与 26.04 锁定策略漂移。"
        echo "  已追加一条 runner 对齐提交。"
    else
        echo "  无需改写。"
    fi
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
    if [ "$N" -gt 0 ]; then printf '  ✓ ai_targets.json 有 %s 只标的\n' "$N"; else printf '  ✗ ai_targets.json targets 为空\n'; FAIL=1; fi
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

"$PYTHON" -m py_compile main.py ci_emit.py src/analyzer.py src/config.py src/notification.py 2>&1 | head -5
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
