"""scripts/check_ci_runners.py 的行尾保真回归。

背景
----
旧版 `--fix` 拿「整行（含行尾符）」去 match，正则的 `(?P<rest>.*)$` 捕不到 `\n`，
于是重写出来的行没有换行符，与下一行粘成一行：

    runs-on: ubuntu-26.04    outputs:

CRLF 文件因为 `.*` 能吃掉 `\r` 而侥幸正常，所以这个缺陷在 Windows 本地完全看不出来，
只在 Linux CI（checkout 出来是 LF）上炸。`scripts/sync_upstream.sh` 第 5 步的 YAML
校验会拦住它，但拦住的代价是每次自动同步都失败。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_ci_runners.py"


def _load_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把脚本按模块加载，并把 WORKFLOW_DIR 指到临时目录。"""
    spec = importlib.util.spec_from_file_location("check_ci_runners_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOW_DIR", workflows)
    return mod, workflows


SAMPLE = """\
name: ci
jobs:
  build:
    name: build
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
  gate:
    runs-on: ubuntu-24.04
    steps:
      - run: echo gate
"""


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_fix_preserves_line_endings_and_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eol: str
) -> None:
    mod, workflows = _load_module(tmp_path, monkeypatch)
    target = workflows / "ci.yml"
    target.write_bytes(SAMPLE.replace("\n", eol).encode())

    problems, changed = mod.scan(target, "ubuntu-26.04", fix=True)

    assert changed is True
    assert len(problems) == 2

    raw = target.read_bytes()
    text = raw.decode()
    # 1) 行数不变：没有任何行被吞掉换行符
    assert text.count(eol) == SAMPLE.count("\n"), "行尾符被破坏，行被粘在一起"
    # 2) runner 已统一
    assert text.count("runs-on: ubuntu-26.04") == 2
    assert "ubuntu-latest" not in text
    # 3) 相邻结构没被破坏
    assert "runs-on: ubuntu-26.04" + eol + "    steps:" in text
    # 4) 别的行一字未动
    assert "      - run: echo gate" in text


@pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_fix_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eol: str) -> None:
    mod, workflows = _load_module(tmp_path, monkeypatch)
    target = workflows / "ci.yml"
    target.write_bytes(SAMPLE.replace("\n", eol).encode())

    mod.scan(target, "ubuntu-26.04", fix=True)
    first = target.read_bytes()
    problems, changed = mod.scan(target, "ubuntu-26.04", fix=True)

    assert problems == []
    assert changed is False
    assert target.read_bytes() == first


def test_check_mode_reports_without_touching_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, workflows = _load_module(tmp_path, monkeypatch)
    target = workflows / "ci.yml"
    original = SAMPLE.encode()
    target.write_bytes(original)

    problems, changed = mod.scan(target, "ubuntu-26.04", fix=False)

    assert changed is False
    assert len(problems) == 2
    assert target.read_bytes() == original


def test_variable_form_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游若采纳 `${{ vars.CI_UBUNTU_RUNNER || '...' }}` 写法，不能被改写。"""
    mod, workflows = _load_module(tmp_path, monkeypatch)
    target = workflows / "ci.yml"
    content = (
        "jobs:\n"
        "  build:\n"
        "    runs-on: ${{ vars.CI_UBUNTU_RUNNER || 'ubuntu-24.04' }}\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    target.write_bytes(content.encode())

    problems, changed = mod.scan(target, "ubuntu-26.04", fix=True)

    assert problems == []
    assert changed is False
    assert target.read_bytes() == content.encode()
