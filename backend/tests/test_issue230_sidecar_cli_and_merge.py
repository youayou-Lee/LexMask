"""Issue#230：MinerU 4.x sidecar 三处 CLI 缺陷的单测。

三缺陷 = ①缺 ``--wait``（CLI 缺省 60s，真卷必超时）②缺 ``--pages``（CLI 默认
只解析前 10 页，静默截断）③``--pages all`` 后多窗口产物撞后端
``mineru4_multiple_docs`` fail-closed（整单作废）。

判据形态：CLI 参数用 fake mineru 替身捕获 ``argv``（不过真模型）；多窗口合并
是纯函数 ``_merge_docvortex_docs``，直接构造窗口 doc 断言；截断校验
（合并后 pages 数 != metadata.page_count）必须任务显式失败，不许静默交付残卷。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

# ---- 纯函数：多窗口合并 ----


def _win(pages: list[int], page_count: int = 149) -> dict:
    page_list = []
    for idx in pages:
        page_list.append({"page_idx": idx, "blocks": [
            {"type": "text", "content": [{"type": "text", "content": f"p{idx}"}]},
        ]})
    return {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "metadata": {"producer": {"name": "mineru", "version": "4.1.3"},
                     "page_count": page_count},
        "pages": page_list,
    }


class TestMergeDocvortexDocs:
    def test_two_windows_merge_full_page_range(self):
        # 149 页真卷实测形态：1-10 + 11-149 两窗口 → 单份 pages=149
        import scripts.mineru_sidecar as sidecar
        merged, warns = sidecar._merge_docvortex_docs([_win(list(range(0, 10))), _win(list(range(10, 149)))])
        assert [p["page_idx"] for p in merged["pages"]] == list(range(149))
        assert merged["metadata"]["page_count"] == 149
        assert merged["schema"] == "docvortex.middle"
        assert warns == []

    def test_duplicate_identical_pages_deduped_not_swallowed(self):
        # doclib 历史缓存页段与当前窗口重叠且逐字节相同 → 去重不吞页
        import scripts.mineru_sidecar as sidecar
        win_a = _win(list(range(0, 10)))
        overlap = json.loads(json.dumps(win_a))
        overlap["pages"] += json.loads(json.dumps(_win(list(range(10, 20)))))["pages"]
        merged, warns = sidecar._merge_docvortex_docs([win_a, overlap])
        assert [p["page_idx"] for p in merged["pages"]] == list(range(20))
        assert any("去重" in w for w in warns)

    def test_conflicting_same_page_idx_fails_loud(self):
        # 同 page_idx 内容不同 = 盲拼会翻倍/吞页的根源 → 显式失败，不许猜
        import scripts.mineru_sidecar as sidecar
        win_a = _win([0])
        win_b = _win([0])
        win_b["pages"][0]["blocks"][0]["content"][0]["content"] = "不同内容"
        with pytest.raises(ValueError, match="page_idx"):
            sidecar._merge_docvortex_docs([win_a, win_b])

    def test_schema_version_mismatch_fails_loud(self):
        import scripts.mineru_sidecar as sidecar
        win_b = _win([10])
        win_b["schema_version"] = "9.9"
        with pytest.raises(ValueError, match="schema_version"):
            sidecar._merge_docvortex_docs([_win([0]), win_b])

    def test_single_doc_returns_unchanged(self):
        import scripts.mineru_sidecar as sidecar
        win = _win([0, 1, 2])
        merged, warns = sidecar._merge_docvortex_docs([win])
        assert merged is win
        assert warns == []


# ---- CLI 参数与端到端（fake mineru）----


def _make_fake_mineru(tmp_path: Path, args_file: Path) -> Path:
    """fake mineru：捕获 argv → 打印 sha → 产物由 doclib 预置夹具承载。"""
    fake = tmp_path / "fake-mineru"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' \"$@\" > {args_file}\n'
        'echo \'{"parse": {"sha256": "abcd", "tier": "flash"}}\'\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then printf "# fake\\n" > "$2"; fi\n'
        "  shift\n"
        "done\n"
        "",
        encoding="utf-8",
    )
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    return fake


def _make_doclib(home: Path, windows: list[dict]) -> None:
    lib = home / "doclib" / "parsed" / "ab" / "abcd" / "flash"
    lib.mkdir(parents=True, exist_ok=True)
    for i, w in enumerate(windows):
        (lib / f"{i}-win_{i}.json").write_text(
            json.dumps(w, ensure_ascii=False), encoding="utf-8"
        )


@pytest.fixture()
def sidecar_mod(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MINERU_SIDECAR_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("MINERU_SIDECAR_BIN", str(tmp_path / "fake-mineru"))
    monkeypatch.setenv("MINERU_SIDECAR_HOME", str(tmp_path / "mineru-home"))
    monkeypatch.setenv("MINERU_SIDECAR_TIER", "flash")
    import importlib

    import scripts.mineru_sidecar as sidecar

    yield importlib.reload(sidecar)


def _run_task(sidecar_mod, tmp_path: Path) -> dict:
    args_file = tmp_path / "cli-args.txt"
    _make_fake_mineru(tmp_path, args_file)
    _make_doclib(tmp_path / "mineru-home", [
        _win(list(range(0, 10))),
        _win(list(range(10, 149))),
    ])
    inp = tmp_path / "in.pdf"
    inp.write_bytes(b"%PDF-fake")
    task_id = "t1"
    sidecar_mod._tasks[task_id] = {"status": "pending", "progress": 0,
                                   "error": None, "warnings": [], "files": ["in.pdf"]}
    sidecar_mod._run_mineru_sync(task_id, [inp])
    task = sidecar_mod._tasks[task_id]
    task["_cli_args"] = args_file.read_text(encoding="utf-8").split()
    return task


class TestCliFlags:
    def test_wait_flag_passed_from_env(self, sidecar_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("MINERU_SIDECAR_WAIT", "1234")
        task = _run_task(sidecar_mod, tmp_path)
        assert task["status"] == "completed", task.get("error")
        args = task["_cli_args"]
        assert args[args.index("--wait") + 1] == "1234"

    def test_pages_all_by_default(self, sidecar_mod, tmp_path):
        task = _run_task(sidecar_mod, tmp_path)
        assert task["status"] == "completed", task.get("error")
        args = task["_cli_args"]
        assert args[args.index("--pages") + 1] == "all"

    def test_pages_env_passthrough(self, sidecar_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("MINERU_SIDECAR_PAGES", "1-5")
        task = _run_task(sidecar_mod, tmp_path)
        assert task["status"] == "completed", task.get("error")
        assert task["_cli_args"][task["_cli_args"].index("--pages") + 1] == "1-5"

    def test_wait_default_follows_timeout_with_margin(self, sidecar_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("MINERU_SIDECAR_TIMEOUT", "7200")
        monkeypatch.delenv("MINERU_SIDECAR_WAIT", raising=False)
        task = _run_task(sidecar_mod, tmp_path)
        assert task["status"] == "completed", task.get("error")
        assert task["_cli_args"][task["_cli_args"].index("--wait") + 1] == "7080"


class TestMultiWindowEndToEnd:
    def test_zip_contains_single_merged_doc(self, sidecar_mod, tmp_path):
        task = _run_task(sidecar_mod, tmp_path)
        assert task["status"] == "completed", task.get("error")
        zip_path = sidecar_mod._task_zip("t1")
        import zipfile

        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.startswith("mineru4_")]
            assert len(names) == 1, names  # 两窗口合并成单份，不盲拼也不丢份
            merged = json.loads(zf.read(names[0]))
        assert len(merged["pages"]) == 149

    def test_truncation_fails_task_explicitly(self, sidecar_mod, tmp_path, monkeypatch):
        # 残卷不许静默出厂：合并后 pages(10) != metadata.page_count(149) ⇒ 任务失败
        monkeypatch.setenv("MINERU_SIDECAR_WORKDIR", str(tmp_path / "work"))
        monkeypatch.setenv("MINERU_SIDECAR_BIN", str(tmp_path / "fake-mineru"))
        monkeypatch.setenv("MINERU_SIDECAR_HOME", str(tmp_path / "mineru-home"))
        monkeypatch.setenv("MINERU_SIDECAR_TIER", "flash")
        import importlib

        import scripts.mineru_sidecar as sidecar

        sidecar = importlib.reload(sidecar)
        args_file = tmp_path / "cli-args.txt"
        _make_fake_mineru(tmp_path, args_file)
        _make_doclib(tmp_path / "mineru-home", [
            _win(list(range(0, 10)), page_count=149),
        ])  # 只有一份 10 页窗口，但 metadata 声称 149 页 ⇒ 截断形态
        inp = tmp_path / "in.pdf"
        inp.write_bytes(b"%PDF-fake")
        sidecar._tasks["t2"] = {"status": "pending", "progress": 0,
                                "error": None, "warnings": [], "files": ["in.pdf"]}
        sidecar._run_mineru_sync("t2", [inp])
        task = sidecar._tasks["t2"]
        assert task["status"] == "failed"
        assert "149" in (task.get("error") or "") and "10" in (task.get("error") or "")
