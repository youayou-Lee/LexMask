"""公开树红线扫描器（Issue#154）回归：非 ASCII 文件名不得被静默漏扫、数目自检必须报红。

背景：`eval-ng/scripts/check_public_tree.py` 曾用不带 `-z` 的 `git diff --name-only` 取改动清单，
非 ASCII 路径被 git 转义成 `"docs/\\345..."` 形态 ⇒ `Path(...).is_file()` 判假 ⇒ 整份中文名文件
被**静默跳过**（门显示「零命中」，实际那几个文件没被看）。

本文件在 tmp_path 里现搭 git 仓，零网络、不碰真实仓。
注意：任何含红线样本字面量的行都必须带 `# public-tree-scan: ignore-line` 标记（本文件自身会被扫描）。
"""

import os
import subprocess
from pathlib import Path

import evalng_bootstrap as boot
import pytest

ptree = boot.load("scripts/check_public_tree.py")

_SAMPLE = "instance 8.8.8.8 path /srv/evidence/arm.json"  # public-tree-scan: ignore-line
_CN_NAME = "docs/开发规范-中文.md"
_SPACE_NAME = "docs/带 空格 的名字.txt"
_GHOST_NAME = "docs/幽灵文件.md"


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "scanner-test",
        "GIT_AUTHOR_EMAIL": "scanner-test@example.invalid",
        "GIT_COMMITTER_NAME": "scanner-test",
        "GIT_COMMITTER_EMAIL": "scanner-test@example.invalid",
    }
    out = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=env,
    )
    return out.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path):
    """一个含中文名改动文件的临时仓：base 提交 + 未提交的中文名红线文件。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("placeholder\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "docs").mkdir(exist_ok=True)
    (root / _CN_NAME).write_text(_SAMPLE + "\n", encoding="utf-8")
    return root, base


class TestNonAsciiPaths:
    def test_changed_files_keeps_raw_non_ascii_names(self, repo):
        """钉子：改动清单必须是**原始**路径，不能是 git 的转义形态（转义形态下游 is_file() 为假）。"""
        root, base = repo
        rels = ptree.changed_files(root, base)
        assert _CN_NAME in rels, rels
        assert not any("\\3" in rel for rel in rels), rels  # 转义形态（旧 bug 症状）
        paths = ptree.default_paths(root, base)
        assert root / _CN_NAME in paths
        assert (root / _CN_NAME).is_file()

    def test_red_line_inside_non_ascii_file_is_reported(self, repo, capsys):
        """AC 场景：只改中文名文件且埋了红线 ⇒ 必须报出该文件并命中（修复前必然「零命中」）。"""
        root, base = repo
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 1, captured
        assert _CN_NAME in captured.out
        assert "public_ip" in captured.out

    def test_clean_non_ascii_and_space_names_are_actually_scanned(self, repo, capsys):
        """反向守卫：干净的中文名/含空格名文件必须**进了扫描集合**（否则「零命中」是假的）。"""
        root, base = repo
        (root / _CN_NAME).write_text("普通中文文档，无红线。\n", encoding="utf-8")
        (root / _SPACE_NAME).write_text("另一个干净文件。\n", encoding="utf-8")
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 0, captured
        # 夹具已有的中文名文件 + 本次新增的空格名文件 = 2 份改动都被扫到（若漏扫只会是 1）
        assert "2 文件，零命中" in captured.out, captured.out


class TestSelfChecks:
    def test_count_mismatch_exits_nonzero(self, repo, capsys, monkeypatch):
        """AC 场景：改动文件数与候选集合不一致 ⇒ 非零退出（宁可红，不可静默漏）。"""
        root, base = repo
        (root / _SPACE_NAME).write_text("干净。\n", encoding="utf-8")
        real = ptree.default_paths
        monkeypatch.setattr(
            ptree, "default_paths",
            lambda r, b: [p for p in real(r, b) if p.name != Path(_SPACE_NAME).name],
        )
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        err = capsys.readouterr().err
        assert rc == 2, err
        assert "不一致" in err, err

    def test_vanished_path_exits_nonzero(self, repo, capsys, monkeypatch):
        """路径解析回归的典型症状（清单里出现但根本不是文件）⇒ 报红。"""
        root, base = repo
        monkeypatch.setattr(ptree, "changed_files", lambda r, b: [_GHOST_NAME])
        monkeypatch.setattr(ptree, "default_paths", lambda r, b: [r / _GHOST_NAME])
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        err = capsys.readouterr().err
        assert rc == 2, err
        assert _GHOST_NAME in err
        assert "路径解析" in err, err

    def test_intentional_skips_are_reported_and_not_fatal(self, repo, capsys):
        """有意跳过（后缀/非 UTF-8）必须**逐类打印原因**，但不判红。"""
        root, base = repo
        (root / _CN_NAME).write_text("干净。\n", encoding="utf-8")
        (root / "docs" / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (root / "docs" / "raw.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        err = capsys.readouterr().err
        assert rc == 0, err
        assert "后缀不在扫描范围" in err and "asset.png" in err, err
        assert "非 UTF-8" in err and "raw.doc" in err, err

    def test_self_test_still_passes(self):
        assert ptree.self_test([]) == 0

    def test_whole_tree_mode_still_works(self, repo, capsys):
        """整树巡检（`--base ""`）不得因个别二进制文件而整体报红（扫的是**已跟踪**文件，故先提交）。"""
        root, _ = repo
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "track non-ascii red-line file")
        rc = ptree.main(["--repo-root", str(root), "--base", ""])
        captured = capsys.readouterr()
        assert rc == 1, captured  # 仓库里就是有红线文件 ⇒ 命中退出 1，但**不是** 2（解析异常）
        assert _CN_NAME in captured.out