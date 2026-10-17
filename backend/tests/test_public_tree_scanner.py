"""公开树红线扫描器（Issue#154）回归：非 ASCII 文件名不得被静默漏扫、数目自检必须报红。

背景：`eval-ng/scripts/check_public_tree.py` 曾用不带 `-z` 的 `git diff --name-only` 取改动清单，
非 ASCII 路径被 git 转义成 `"docs/\\345..."` 形态 ⇒ `Path(...).is_file()` 判假 ⇒ 整份中文名文件
被**静默跳过**（门显示「零命中」，实际那几个文件没被看）。

覆盖面（含门⑥ 冷审第二段抓出的同类路径）：
- `-z`/NUL 口径与中文/空格/重音/引号文件名（A1/A2/A3/A5）
- **git 取清单失败必须报红**，不得与「没有改动」混淆（I-1）
- **重命名 R 必须进扫描集**（I-2）
- 自检②「候选路径不是普通文件」⇒ exit 2；自检③未解释余数对账（I-3）
- `--paths` 模式校验、空区间与 git 失败的文案区分（M-6/M-7）
- C locale 下打印中文路径不崩（C2）

第三轮增量复核（With fixes）补的覆盖：
- **已跟踪文件改了但没提交**也必须进扫描集（I2，`diff -z … HEAD` 那条命令）
- `--exclude-standard` 必须留着：`.gitignore` 掉的路径一律不进扫描集（M1）
- `AMRT` **不含 `D`**：删除的文件不进扫描集，否则任何删文件的 PR 假红（M2）
- 同一轮里既有命中又有坏路径时，自检② 的「路径解析」文案不得被绕过（M3）
- 非 UTF-8 文件系统编码下的警告，只在候选集合含非 ASCII 名时才打（M4）

本文件在 tmp_path 里现搭 git 仓，零网络、不碰真实仓。
注意：任何含红线样本字面量的行都必须带**按规则点名**的放过标记
（`# public-tree-scan: ignore-line(<规则名>, …)`）；裸标记自 Issue#160 起不放过任何规则。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import evalng_bootstrap as boot
import pytest

ptree = boot.load("scripts/check_public_tree.py")

_SAMPLE = "instance 8.8.8.8 path /srv/evidence/arm.json"  # public-tree-scan: ignore-line(abs_path, public_ip)
_CN_NAME = "docs/开发规范-中文.md"
_SPACE_NAME = "docs/带 空格 的名字.txt"
_ACCENT_NAME = "docs/重音-café-ü.md"
_QUOTE_NAME = 'docs/引号"名.md'
_WS_NAME = " "  # 名字全是空白：不得被 strip() 谓词丢掉（冷审 M-1）
_GHOST_NAME = "docs/幽灵文件.md"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "eval-ng" / "scripts" / "check_public_tree.py"


def _git_env(root: Path, *, c_locale: bool = False, ascii_fs: bool = False) -> dict:
    env = {
        **os.environ,
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "scanner-test",
        "GIT_AUTHOR_EMAIL": "scanner-test@example.invalid",
        "GIT_COMMITTER_NAME": "scanner-test",
        "GIT_COMMITTER_EMAIL": "scanner-test@example.invalid",
    }
    # 环境残留会让 `git -C <tmp>` 指向另一个仓（冷审 M-8）
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    if c_locale:
        env.update({"LC_ALL": "C", "LANG": "C"})
    if ascii_fs:
        # 关掉 PEP 538 的 locale 强制转换与 PEP 540 的 UTF-8 模式 ⇒ 文件系统编码退化成 ASCII
        env.update({"PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"})
    return env


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True, env=_git_env(root),
    )
    return out.stdout.strip()


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture()
def repo(tmp_path: Path):
    """一个含中文名改动文件的临时仓：base 提交 + 未提交的中文名红线文件。"""
    if shutil.which("git") is None:  # 缺 git 必须显式可见（冷审 M-8）
        pytest.skip("环境无 git，公开树扫描器用例跳过")
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

    def test_accent_and_quote_names_are_scanned(self, repo, capsys):
        """A5/A3：重音字符与**含双引号**的名字也必须进扫描集（后者钉住「必须走 `-z`」而非 quotepath）。"""
        root, base = repo
        (root / _CN_NAME).write_text("干净。\n", encoding="utf-8")
        (root / _ACCENT_NAME).write_text("干净。\n", encoding="utf-8")
        (root / _QUOTE_NAME).write_text(_SAMPLE + "\n", encoding="utf-8")
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 1, captured
        assert _QUOTE_NAME in captured.out, captured.out
        assert "3 文件，命中" in captured.err, captured.err  # 三份都进了扫描集合

    def test_whitespace_only_filename_is_not_dropped(self, repo, capsys):
        """M-1：名字全是空白的文件不得被 `strip()` 谓词丢掉（丢掉=静默漏扫）。"""
        root, base = repo
        (root / _CN_NAME).write_text("干净。\n", encoding="utf-8")
        (root / "docs" / "plain.txt").write_text("干净。\n", encoding="utf-8")
        (root / _WS_NAME).write_text(_SAMPLE + "\n", encoding="utf-8")
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 1, captured
        assert "public_ip" in captured.out, captured.out
        assert "3 文件，命中" in captured.err, captured.err

    def test_committed_non_ascii_change_is_scanned_via_base_range(self, repo, capsys):
        """M-4：Issue 自己那条触发路径（已提交区间 `<base>...HEAD`）也要有非 ASCII 覆盖。"""
        root, base = repo
        _commit_all(root, "add non-ascii red-line file")
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 1, captured
        assert _CN_NAME in captured.out, captured.out

    def test_renamed_file_is_still_scanned(self, repo, capsys):
        """I-2：`git mv` + 改内容（R0xx）必须进扫描集——旧 `--diff-filter=AM` 会整体丢弃它。"""
        root, base = repo
        old = root / "docs" / "旧名.md"
        old.write_text("".join(f"filler line {i}\n" for i in range(40)), encoding="utf-8")
        _commit_all(root, "base filler")
        base = _git(root, "rev-parse", "HEAD")
        _git(root, "mv", "docs/旧名.md", "docs/新名.md")
        (root / "docs" / "新名.md").write_text(
            "".join(f"filler line {i}\n" for i in range(40)) + _SAMPLE + "\n", encoding="utf-8",
        )
        _commit_all(root, "rename + red line")
        rels = ptree.changed_files(root, base)
        assert "docs/新名.md" in rels, rels
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 1, captured
        assert "docs/新名.md" in captured.out, captured.out


class TestSelfChecks:
    def test_count_mismatch_exits_nonzero(self, repo, capsys, monkeypatch):
        """内部不变量：`default_paths` 与 `changed_files` 口径漂移 ⇒ 非零退出。"""
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

    def test_bogus_base_fails_loud_and_never_reports_clean(self, repo, capsys):
        """I-1：base 写错/不可解析时**必须报红**，绝不能「绿着放行」（旧实现把 git 失败吞成空列表）。"""
        root, _ = repo
        _commit_all(root, "add non-ascii red-line file")
        rc = ptree.main(["--repo-root", str(root), "--base", "deadbeefdeadbeef"])
        captured = capsys.readouterr()
        assert rc == 2, captured
        assert "取改动清单失败" in captured.err, captured.err
        assert "零命中" not in captured.out, captured.out

    def test_empty_range_message_differs_from_git_failure(self, repo, capsys):
        """M-7：空区间（真的没有改动）与 git 失败的文案必须可区分。"""
        root, _ = repo
        _commit_all(root, "commit everything")
        rc = ptree.main(["--repo-root", str(root), "--base", "HEAD"])
        captured = capsys.readouterr()
        assert rc == 2, captured
        assert "没有候选文件" in captured.err, captured.err
        assert "取改动清单失败" not in captured.err, captured.err
        rc = ptree.main(["--repo-root", str(root), "--base", "deadbeefdeadbeef"])
        other = capsys.readouterr()
        assert rc == 2, other
        assert "取改动清单失败" in other.err, other.err

    def test_paths_mode_scans_given_paths_and_flags_missing(self, repo, capsys):
        """M-6：`--paths` 模式两个方向都要成立（给了就扫；给了不存在 ⇒ 红）。"""
        root, _ = repo
        clean = root / "docs" / "clean.txt"
        clean.write_text("干净。\n", encoding="utf-8")
        rc = ptree.main(["--repo-root", str(root), "--base", "", "--paths", str(clean)])
        captured = capsys.readouterr()
        assert rc == 0, captured
        assert "1 文件，零命中" in captured.out, captured.out
        rc = ptree.main(["--repo-root", str(root), "--base", "", "--paths", str(root / _GHOST_NAME)])
        err = capsys.readouterr().err
        assert rc == 2, err
        assert _GHOST_NAME in err, err

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

    def test_unexplained_candidates_counts_only_explained_skips(self):
        """自检③的纯函数：`not_a_file` 不算「已解释」，任何没说明的跳过都会留成余数。"""
        skipped = {"skip_suffix": ["a.png"], "too_large": [], "unreadable": ["b.doc"], "not_a_file": ["c"]}
        assert ptree.unexplained_candidates(4, 2, skipped) == 0  # 2 扫描 + 1 后缀 + 1 非 UTF-8 = 4
        assert ptree.unexplained_candidates(4, 1, skipped) == 1  # 少扫一个 ⇒ 余数 1
        assert ptree.unexplained_candidates(3, 1, skipped) == 0
        assert ptree.unexplained_candidates(3, 3, skipped) == -2  # 多算也要判红（main 用 != 0）

    def test_unexplained_remainder_wiring_exits_nonzero(self, repo, capsys, monkeypatch):
        """接线用例（人为制造余数）：`main` 必须真的把非零余数判红。

        诚实边界：生产路径上余数只可能来自**未来新增的、没登记的跳过类别**（今天的每条跳过路径都计数），
        所以这条只能用桩制造余数——它钉住的是「接线没被删、没被改成恒真」，不是「今天能触发」。
        """
        root, base = repo
        (root / _CN_NAME).write_text("干净。\n", encoding="utf-8")
        monkeypatch.setattr(ptree, "unexplained_candidates", lambda *a, **k: 1)
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        err = capsys.readouterr().err
        assert rc == 2, err
        assert "未解释的候选文件" in err, err

    def test_whole_tree_mode_still_works(self, repo, capsys):
        """整树巡检（`--base ""`）不得因个别二进制文件而整体报红（扫的是**已跟踪**文件，故先提交）。"""
        root, _ = repo
        _commit_all(root, "track non-ascii red-line file")
        rc = ptree.main(["--repo-root", str(root), "--base", ""])
        captured = capsys.readouterr()
        assert rc == 1, captured  # 仓库里就是有红线文件 ⇒ 命中退出 1，但**不是** 2（解析异常）
        assert _CN_NAME in captured.out

    def test_c_locale_run_prints_non_ascii_without_crashing(self, repo):
        """C2：`LC_ALL=C` 下跑真进程，打印中文路径不得抛 UnicodeEncodeError（崩=红在无关问题上）。"""
        root, base = repo
        _commit_all(root, "add non-ascii red-line file")
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--repo-root", str(root), "--base", base],
            capture_output=True, text=True, env=_git_env(root, c_locale=True),
        )
        combined = proc.stdout + proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr
        assert _CN_NAME in combined, combined  # 中文路径确实被打印出来了
        assert proc.returncode in (1, 2), (proc.returncode, combined)
        if proc.returncode == 2:  # 该环境的文件系统编码非 UTF-8 ⇒ 必须说清原因，不许含糊报「路径解析可疑」
            assert "文件系统编码" in proc.stderr and "PYTHONUTF8" in proc.stderr, proc.stderr

    def test_ascii_filesystem_encoding_is_reported_loudly(self, repo):
        """M-5/C2 的真实限制：文件系统编码退化成 ASCII 时，必须**响铃说明原因**而不是含糊其辞。

        `LC_ALL=C` + `PYTHONUTF8=0` + `PYTHONCOERCECLOCALE=0` 下中文名无法 stat（`is_file()` 吞掉
        `UnicodeEncodeError`）⇒ 这不是静默漏扫（rc 2），但文案要能指向真因与补救办法。
        """
        root, base = repo
        _commit_all(root, "add non-ascii red-line file")
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--repo-root", str(root), "--base", base],
            capture_output=True, text=True, env=_git_env(root, c_locale=True, ascii_fs=True),
        )
        assert "Traceback" not in proc.stderr, proc.stderr
        assert _CN_NAME in proc.stderr, proc.stderr
        if proc.returncode == 2:  # ASCII 文件系统编码确实生效
            assert "文件系统编码" in proc.stderr, proc.stderr
            assert "PYTHONUTF8" in proc.stderr, proc.stderr
            assert "零命中" not in proc.stdout, proc.stdout
        else:  # 有些 Python 版本/平台仍给 UTF-8 ⇒ 至少不许静默说干净
            assert proc.returncode == 1, (proc.returncode, proc.stdout)


class TestThirdRoundFollowups:
    """门⑥ 增量复核（第三轮）抓出的覆盖缺口与文案瑕疵，逐条配钉子。"""

    def test_uncommitted_tracked_change_is_scanned(self, repo, capsys):
        """I2：`diff -z … HEAD` 那条命令 —— **已跟踪文件改了但没提交**也必须进扫描集。

        这条命令就是 docstring 点名的「门禁在提交前就能跑」；第三轮冷审实测把它整条删掉仍 19/19 全绿。
        这里把基线取在提交**之后**，让 `<base>...HEAD` 与未跟踪清单都为空，只有它能把文件捞回来。
        """
        root, _ = repo
        (root / _CN_NAME).write_text("干净。\n", encoding="utf-8")
        _commit_all(root, "track clean non-ascii file")
        base = _git(root, "rev-parse", "HEAD")  # 基线 = 提交后 ⇒ 只剩工作区改动
        (root / _CN_NAME).write_text("干净。\n" + _SAMPLE + "\n", encoding="utf-8")
        assert ptree.changed_files(root, base) == [_CN_NAME], ptree.changed_files(root, base)
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 1, captured
        assert _CN_NAME in captured.out, captured.out
        assert "1 文件，命中" in captured.err, captured.err

    def test_gitignored_files_are_not_scanned(self, repo, capsys):
        """M1：`--exclude-standard` 必须留着 —— `.gitignore` 掉的路径不进扫描集（否则假红 + 巨慢）。"""
        root, _ = repo
        (root / _CN_NAME).write_text("干净。\n", encoding="utf-8")
        (root / ".gitignore").write_text("docs/ignored-note.md\n", encoding="utf-8")
        _commit_all(root, "track clean non-ascii file and gitignore")
        base = _git(root, "rev-parse", "HEAD")
        (root / "docs" / "ignored-note.md").write_text(_SAMPLE + "\n", encoding="utf-8")  # 埋雷但被忽略
        (root / "docs" / "clean-note.md").write_text("干净。\n", encoding="utf-8")  # 保证候选集合非空
        rels = ptree.changed_files(root, base)
        assert "docs/ignored-note.md" not in rels, rels
        assert "docs/clean-note.md" in rels, rels
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 0, captured
        assert "ignored-note.md" not in captured.out + captured.err, captured
        assert "1 文件，零命中" in captured.out, captured.out

    def test_deleted_file_is_not_a_parse_error(self, repo, capsys):
        """M2：`--diff-filter=AMRT` **不含 `D`** —— 删除的文件不进扫描集，否则任何删文件的 PR 一律假红。"""
        root, _ = repo
        (root / "docs" / "obsolete.md").write_text("将被删除。\n", encoding="utf-8")
        _commit_all(root, "track file to delete")
        base = _git(root, "rev-parse", "HEAD")
        (root / _CN_NAME).write_text("干净。\n", encoding="utf-8")
        _git(root, "rm", "-q", "docs/obsolete.md")
        _commit_all(root, "modify non-ascii + delete obsolete")
        rels = ptree.changed_files(root, base)
        assert "docs/obsolete.md" not in rels, rels
        assert _CN_NAME in rels, rels
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        captured = capsys.readouterr()
        assert rc == 0, captured
        assert "路径解析" not in captured.err, captured.err

    def test_self_check2_message_survives_findings(self, repo, capsys, monkeypatch):
        """M3：自检② 的早退不得被 `and not findings` 这类改写绕过 —— 文案仍须指向「路径解析」。"""
        root, base = repo
        monkeypatch.setattr(ptree, "changed_files", lambda r, b: [_GHOST_NAME, _CN_NAME])
        monkeypatch.setattr(ptree, "default_paths", lambda r, b: [r / _GHOST_NAME, r / _CN_NAME])
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        err = capsys.readouterr().err
        assert rc == 2, err
        assert "路径解析" in err, err  # 同时有命中时，也仍要说清是「路径解析可疑」
        assert "未解释的候选文件" not in err, err

    def test_fs_encoding_warning_only_when_non_ascii_candidates(self, repo, capsys, monkeypatch):
        """M4：非 UTF-8 文件系统编码下，**全 ASCII 的树不该被吓人警告**（噪声），有非 ASCII 名时才提示。"""
        root, base = repo
        monkeypatch.setattr(ptree, "_filesystem_encoding_note", lambda: "文件系统编码是 ascii（测试替身）")
        (root / _CN_NAME).unlink()  # 夹具那个中文名候选去掉 ⇒ 树内全 ASCII
        (root / "docs" / "plain.md").write_text("干净。\n", encoding="utf-8")
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        err = capsys.readouterr().err
        assert rc == 0, err
        assert "文件系统编码" not in err, err
        (root / _ACCENT_NAME).write_text("干净。\n", encoding="utf-8")  # 出现非 ASCII 名 ⇒ 必须提示
        rc = ptree.main(["--repo-root", str(root), "--base", base])
        err = capsys.readouterr().err
        assert rc == 0, err
        assert "文件系统编码" in err, err


def test_tracked_public_tree_has_no_credential_findings():
    """Issue#140 / #141 AC2 的回归钉：**已入库**文件里不得再出现任何 `credential_*` 命中。

    只扫 `git ls-files`（= 真正的「公开树」，确定性、不受本地未跟踪产物影响），跳过口径与 CLI
    一致（后缀表 / 超上限 / 非 UTF-8），并复用 `scan_source` 以尊重 `ignore-line` 放过标记
    （扫描器自身的阳性对照夹具靠它豁免）。诚实地雷：本文件是被扫对象之一，故新增任何含凭据
    形态字面量的行都必须带标记。
    """
    root = _REPO_ROOT
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    ).stdout
    offenders: list[str] = []
    for rel in [item for item in listed.split("\0") if item]:
        path = root / rel
        if path.suffix.lower() in ptree.SKIP_SUFFIXES or not path.is_file():
            continue
        if path.stat().st_size > ptree.MAX_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        offenders.extend(
            line
            for line in ptree.scan_source(rel, text, denylist=[], allow_prefixes=[])
            if "[credential" in line
        )
    assert offenders == [], "公开树出现凭据痕迹（Issue#140/#141 回归）：\n" + "\n".join(offenders)


# --- 门①.5 r3：接线钉（防「删一行就全绿」） -------------------------------------
# r3 自造变异证明：删掉 `problems += _extension_coverage_check()` 后 self-test 与 25 条单测
# **全绿**（SURVIVED-BOTH），再叠加 `SKIP_SUFFIXES += .ps1` 仍然全绿 ⇒ 后缀/体积这道唯一
# 防线可被无声拆掉。下面三条把「helper 真的被 self_test 调用」钉死，第四条把 `MAX_BYTES`
# 的**使用点**（而不只是常量）钉死。


def test_self_test_wires_extension_coverage_check(monkeypatch):
    relaxed = frozenset(set(ptree.SKIP_SUFFIXES) | {".ps1"})
    monkeypatch.setattr(ptree, "SKIP_SUFFIXES", relaxed)
    assert ptree._extension_coverage_check(), "直接调用必须报出未被扫描的 .ps1 夹具"
    assert ptree.self_test([]) == 1, "self_test 未把 _extension_coverage_check 的结论算进 rc"


def test_self_test_wires_scan_scope_contract(monkeypatch):
    monkeypatch.setattr(ptree, "MAX_BYTES", 8 * 1024)
    assert ptree._scan_scope_contract(), "上限低于地板时必须报出"
    assert ptree.self_test([]) == 1, "self_test 未把 _scan_scope_contract 的结论算进 rc"


def test_self_test_wires_end_to_end_scope_check(monkeypatch):
    monkeypatch.setattr(ptree, "main", lambda argv=None, **kwargs: 0)
    assert ptree._end_to_end_scope_check(), "main 恒 0 时必须报出端到端对照失败"
    assert ptree.self_test([]) == 1, "self_test 未把 _end_to_end_scope_check 的结论算进 rc"


def test_self_test_wires_ignore_line_scope_check(monkeypatch):
    """门①.5 r1 M-1：`_ignore_line_scope_check()` 的调用被删掉时 self_test 必须仍判红。

    删掉调用后本测试的 monkeypatch 无处生效（返回值不再被并入 problems）⇒ rc=0 ⇒ 本用例失败。
    """
    monkeypatch.setattr(ptree, "_ignore_line_scope_check", lambda: ["forced-problem"])
    assert ptree.self_test([]) == 1, "self_test 未把 _ignore_line_scope_check 的结论算进 rc"


def test_ignore_line_scope_check_detects_a_silenced_scanner(monkeypatch):
    """门①.5 r2 R2-2：把 `scan_source` 静默成空 ⇒ 该契约必须报出问题。

    否则函数体被掏空（`return []`）时调用点接线钉仍绿——与 #159 r3 同类空转。
    """
    monkeypatch.setattr(ptree, "scan_source", lambda *args, **kwargs: [])
    problems = ptree._ignore_line_scope_check()
    assert problems, "扫描器被静默时放过标记契约仍报「无问题」"
    assert any("放过标记过宽" in item for item in problems), problems


def test_empty_parentheses_marker_suppresses_nothing():
    """门①.5 r1 M-2：`ignore-line()`（空括号）的规则集为空 ⇒ 什么都不放过（不是整行豁免）。"""
    line = 'cfg password="Ab12Cd34" host 8.8.8.8  # ' + ptree.IGNORE_MARKER + "()"  # public-tree-scan: ignore-line(public_ip, credential_literal)
    rules = sorted(ptree.scan_source("empty-parens", line, denylist=[], allow_prefixes=[]))
    assert rules, "空括号标记不得整行豁免"
    joined = "\n".join(rules)
    assert "credential_literal" in joined and "public_ip" in joined, joined
    assert ptree._ignore_line_scope_check() == []


def test_max_bytes_is_pinned_at_the_use_site(monkeypatch):
    """契约钉常量（`MAX_BYTES >= 地板`），使用点 `size > MAX_BYTES` 也要钉。"""
    monkeypatch.setattr(ptree, "MAX_BYTES", ptree._OVERSIZED_FIXTURE_BYTES - 1)
    problems = ptree._extension_coverage_check()
    assert any("oversized_probe.txt" in item for item in problems), (
        "把上限压到超限夹具之下时必须报出该夹具未被扫描，否则使用点可被改坏而不自知"
    )


def test_self_test_reports_a_case_that_stops_hitting(monkeypatch):
    """钉 `if not hits:` 这条逐例判据本身（r3 变异 `if not hits:`→`if False:` 曾 SURVIVED）。"""
    monkeypatch.setattr(
        ptree,
        "SELF_TEST_BAD_CASES",
        tuple(ptree.SELF_TEST_BAD_CASES) + (("no-hit-probe", "普通说明文字，没有键值对。"),),
    )
    assert ptree.self_test([]) == 1, "停打的阳性对照用例必须让 self-test 判红"


def test_self_test_reports_a_declared_rule_without_coverage(monkeypatch):
    """钉「登记表 − 实际覆盖」这条判据（r3 变异 `missing = [...]`→`[]` 曾 SURVIVED）。"""
    monkeypatch.setattr(
        ptree, "KNOWN_RULE_IDS", frozenset(set(ptree.KNOWN_RULE_IDS) | {"phantom_rule"})
    )
    assert ptree.self_test([]) == 1, "登记了但从无命中的规则 id 必须让 self-test 判红"


def test_finding_line_rule_discriminates():
    """钉汇总行形状的正则本身（r3 变异把它改成 `r""` 会让一切形状断言空转）。"""
    assert ptree._FINDING_LINE_RE.match("docs/x.md:12: [credential_literal] value")
    assert not ptree._FINDING_LINE_RE.match("docs/x.md:12: 没有规则标记的一行")


def test_cjk_valued_credentials_hit_and_chinese_prose_does_not():
    """Issue#160 AC1/AC2：中文取值口令必须命中；中文正文/日期不得误报。"""
    hit_rules = {
        rule for rule, _v in ptree.scan('cfg password="测试口令123"', denylist=[], allow_prefixes=[])  # public-tree-scan: ignore-line(credential_literal)
    }
    assert "credential_literal" in hit_rules
    bare_rules = {
        rule for rule, _v in ptree.scan("cfg password=测试口令123", denylist=[], allow_prefixes=[])  # public-tree-scan: ignore-line(credential_bare)
    }
    assert bare_rules == {"credential_bare"}, bare_rules
    for clean in (
        'SECRET_TEXT = "委托人陈文清，身份证号110101199001011234。"',
        'cfg key="2023年6月15日"',
        'cfg secret = "本院认为合同有效"',
        "max_new_tokens=2048：生产预算公式",
    ):
        assert ptree.scan(clean, denylist=[], allow_prefixes=[]) == [], clean


def test_ignore_line_marker_is_rule_scoped():
    """Issue#160 AC3：放过标记只放过点名规则；裸标记不放过任何规则。"""
    scoped = 'cfg password="Ab12Cd34" host 8.8.8.8  # ' + f"{ptree.IGNORE_MARKER}(credential_literal)"  # public-tree-scan: ignore-line(public_ip)
    rules = {
        line.split("[", 1)[1].split("]", 1)[0]
        for line in ptree.scan_source("scope", scoped, denylist=[], allow_prefixes=[])
    }
    assert "credential_literal" not in rules and "public_ip" in rules, rules
    bare = 'cfg password="Ab12Cd34" host 8.8.8.8  # ' + ptree.IGNORE_MARKER  # public-tree-scan: ignore-line(public_ip)
    bare_rules = {
        line.split("[", 1)[1].split("]", 1)[0]
        for line in ptree.scan_source("bare", bare, denylist=[], allow_prefixes=[])
    }
    assert {"credential_literal", "public_ip"} <= bare_rules, bare_rules
