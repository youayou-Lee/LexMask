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
    # 门⑥ I1：TIMEOUT 必须用非缺省值并在 reload 前设置——reload 冻结模块级
    # MINERU_TIMEOUT_SEC，测试体内 setenv 对已加载模块是死代码（缺省 7200 时
    # 「4880」断言与纯缺省路径重合，把实现改读错变量名照样绿）。
    monkeypatch.setenv("MINERU_SIDECAR_TIMEOUT", "5000")
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

    def test_pages_env_passthrough_partial_range_relaxes_gate(self, sidecar_mod, tmp_path, monkeypatch):
        # 门⑥ I2：部分 range 是开发调试旋钮——真实 MinerU 在 --pages 1-5 下只产
        # 5 页窗口，与「残卷不许出厂」校验直接冲突。口径：pages != all 时截断
        # 校验放宽为显式 warning（不静默、也不误杀故意的部分解析）。
        monkeypatch.setenv("MINERU_SIDECAR_PAGES", "1-5")
        args_file = tmp_path / "cli-args.txt"
        _make_fake_mineru(tmp_path, args_file)
        _make_doclib(tmp_path / "mineru-home", [
            _win(list(range(0, 5)), page_count=5),  # 窗口 5 页，metadata 也是 5
        ])
        inp = tmp_path / "in.pdf"
        inp.write_bytes(b"%PDF-fake")
        sidecar_mod._tasks["t3"] = {"status": "pending", "progress": 0,
                                    "error": None, "warnings": [], "files": ["in.pdf"]}
        sidecar_mod._run_mineru_sync("t3", [inp])
        task = sidecar_mod._tasks["t3"]
        assert task["status"] == "completed", task.get("error")
        task["_cli_args"] = args_file.read_text(encoding="utf-8").split()
        assert task["_cli_args"][task["_cli_args"].index("--pages") + 1] == "1-5"

    def test_pages_env_partial_with_full_page_count_warns_not_fails(self, sidecar_mod, tmp_path, monkeypatch):
        # 真实可达的最坏组合：用户传 1-5 但 metadata.page_count 报全文页数 ⇒
        # 放宽为 warning，不许任务失败，也不许静默（warning 必须点名两者）
        monkeypatch.setenv("MINERU_SIDECAR_PAGES", "1-5")
        args_file = tmp_path / "cli-args.txt"
        _make_fake_mineru(tmp_path, args_file)
        _make_doclib(tmp_path / "mineru-home", [
            _win(list(range(0, 5)), page_count=149),
        ])
        inp = tmp_path / "in.pdf"
        inp.write_bytes(b"%PDF-fake")
        sidecar_mod._tasks["t4"] = {"status": "pending", "progress": 0,
                                    "error": None, "warnings": [], "files": ["in.pdf"]}
        sidecar_mod._run_mineru_sync("t4", [inp])
        task = sidecar_mod._tasks["t4"]
        assert task["status"] == "completed", task.get("error")
        assert any("1-5" in w and "149" in w for w in task["warnings"])

    def test_wait_default_follows_timeout_with_margin(self, sidecar_mod, tmp_path, monkeypatch):
        monkeypatch.delenv("MINERU_SIDECAR_WAIT", raising=False)
        task = _run_task(sidecar_mod, tmp_path)
        assert task["status"] == "completed", task.get("error")
        # fixture 在 reload 前 setenv TIMEOUT=5000 ⇒ 缺省 wait = 5000-120 = 4880
        assert task["_cli_args"][task["_cli_args"].index("--wait") + 1] == "4880"


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

    def test_missing_page_count_warns_not_silent(self, sidecar_mod, tmp_path):
        # 门⑥ M3：metadata.page_count 缺失时校验跳过是 fail-open——必须留 warning
        # 使「页数口径不可证」显式可见（不失败：下游 shortfall 守卫兜底）
        args_file = tmp_path / "cli-args.txt"
        _make_fake_mineru(tmp_path, args_file)
        win = _win(list(range(0, 10)), page_count=149)
        del win["metadata"]["page_count"]  # 4.1.3 部分产物实测缺失该字段
        _make_doclib(tmp_path / "mineru-home", [win])
        inp = tmp_path / "in.pdf"
        inp.write_bytes(b"%PDF-fake")
        sidecar_mod._tasks["t5"] = {"status": "pending", "progress": 0,
                                    "error": None, "warnings": [], "files": ["in.pdf"]}
        sidecar_mod._run_mineru_sync("t5", [inp])
        task = sidecar_mod._tasks["t5"]
        assert task["status"] == "completed", task.get("error")
        assert any("page_count" in w for w in task["warnings"])


# ---- 门⑥挂账收口（2026-11-04）：F1 同 stem 覆盖 / F4 WAIT fail-fast ----


def _make_fake_mineru_input_aware(tmp_path: Path, args_file: Path) -> Path:
    """fake mineru：md 内容带输入名——同 stem 覆盖测试需要两个 md 可区分。"""
    fake = tmp_path / "fake-mineru"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > {args_file}\n'
        'echo \'{"parse": {"sha256": "abcd", "tier": "flash"}}\'\n'
        'out=""\n'
        'prev=""\n'
        'input=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-o" ]; then out="$a"; fi\n'
        '  prev="$a"; input="$a"\n'
        "done\n"
        'printf "# fake %s\\n" "$input" > "$out"\n'
        "",
        encoding="utf-8",
    )
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    return fake


def test_same_stem_two_inputs_no_overwrite(sidecar_mod, tmp_path, monkeypatch):
    # 门⑥ M4（挂账①）：一任务两输入同 stem（a.pdf + a.doc，供料批量含 PDF+doc）
    # ⇒ md 与 mineru4 json 都必须各自存在，不互相覆盖
    args_file = tmp_path / "cli-args.txt"
    _make_fake_mineru_input_aware(tmp_path, args_file)
    _make_doclib(tmp_path / "mineru-home", [_win([0, 1], page_count=2)])
    a_pdf = tmp_path / "a.pdf"
    a_doc = tmp_path / "a.doc"
    a_pdf.write_bytes(b"%PDF-a")
    a_doc.write_bytes(b"%DOC-a")
    sidecar_mod._tasks["t6"] = {"status": "pending", "progress": 0,
                                "error": None, "warnings": [],
                                "files": ["a.pdf", "a.doc"]}
    sidecar_mod._run_mineru_sync("t6", [a_pdf, a_doc])
    task = sidecar_mod._tasks["t6"]
    assert task["status"] == "completed", task.get("error")
    import zipfile

    with zipfile.ZipFile(sidecar_mod._task_zip("t6")) as zf:
        names = sorted(zf.namelist())
        mds = [n for n in names if n.endswith(".md")]
        js = [n for n in names if n.startswith("mineru4_")]
        assert len(mds) == 2, names
        assert len(js) == 2, names
        assert len(set(mds)) == 2 and len(set(js)) == 2
        body = {n: zf.read(n).decode("utf-8", "replace") for n in mds}
        assert len(set(body.values())) == 2  # 两个 md 内容可区分 = 没有覆盖


def test_invalid_wait_env_fails_task_readably(sidecar_mod, tmp_path, monkeypatch):
    # 门⑥ M6（挂账④）：MINERU_SIDECAR_WAIT 非数字 ⇒ 任务失败且报错点名变量名
    monkeypatch.setenv("MINERU_SIDECAR_WAIT", "abc")
    args_file = tmp_path / "cli-args.txt"
    _make_fake_mineru(tmp_path, args_file)
    inp = tmp_path / "in.pdf"
    inp.write_bytes(b"%PDF-fake")
    sidecar_mod._tasks["t7"] = {"status": "pending", "progress": 0,
                                "error": None, "warnings": [], "files": ["in.pdf"]}
    sidecar_mod._run_mineru_sync("t7", [inp])
    task = sidecar_mod._tasks["t7"]
    assert task["status"] == "failed"
    assert "MINERU_SIDECAR_WAIT" in (task.get("error") or "")
    assert "could not convert" not in (task.get("error") or "")


def test_disambiguated_name_colliding_with_real_stem_no_overwrite(sidecar_mod, tmp_path):
    # 门⑥ r2 M1：消歧名 a_1 恰撞上另一输入的真实 stem（a_1.rtf）⇒ 必须继续
    # 递增到 a_2，第 2/3 输入的 md 与 mineru4 json 不许静默互相覆盖
    args_file = tmp_path / "cli-args.txt"
    _make_fake_mineru_input_aware(tmp_path, args_file)
    _make_doclib(tmp_path / "mineru-home", [_win([0], page_count=1)])
    files = []
    for fn in ("a.pdf", "a.doc", "a_1.rtf"):
        f = tmp_path / fn
        f.write_bytes(b"X" + fn.encode())
        files.append(f)
    sidecar_mod._tasks["t8"] = {"status": "pending", "progress": 0,
                                "error": None, "warnings": [],
                                "files": [f.name for f in files]}
    sidecar_mod._run_mineru_sync("t8", files)
    task = sidecar_mod._tasks["t8"]
    assert task["status"] == "completed", task.get("error")
    import zipfile

    with zipfile.ZipFile(sidecar_mod._task_zip("t8")) as zf:
        names = sorted(zf.namelist())
        mds = [n for n in names if n.endswith(".md")]
        js = [n for n in names if n.startswith("mineru4_")]
        assert len(mds) == 3 and len(set(mds)) == 3, names
        assert len(js) == 3 and len(set(js)) == 3, names
        body = {n: zf.read(n).decode("utf-8", "replace") for n in mds}
        assert len(set(body.values())) == 3


def test_invalid_wait_env_inf_also_named(sidecar_mod, tmp_path, monkeypatch):
    # 门⑥ r2 M2：MINERU_SIDECAR_WAIT=inf 走 OverflowError，同样要点名变量
    monkeypatch.setenv("MINERU_SIDECAR_WAIT", "inf")
    args_file = tmp_path / "cli-args.txt"
    _make_fake_mineru(tmp_path, args_file)
    inp = tmp_path / "in.pdf"
    inp.write_bytes(b"%PDF-fake")
    sidecar_mod._tasks["t9"] = {"status": "pending", "progress": 0,
                                "error": None, "warnings": [], "files": ["in.pdf"]}
    sidecar_mod._run_mineru_sync("t9", [inp])
    task = sidecar_mod._tasks["t9"]
    assert task["status"] == "failed"
    assert "MINERU_SIDECAR_WAIT" in (task.get("error") or "")


def test_non_pdf_input_omits_pages_flag(sidecar_mod, tmp_path):
    # 验收实弹抓到的回归：MinerU CLI 对非 PDF 输入拒绝 --pages（page_range_invalid）
    # —— #234 缺省 all 会打死所有 .doc 喂料任务。口径：仅 PDF 传 --pages，
    # 非 PDF 不传（上游对 doc 本就全文档解析，无截断问题）。
    args_file = tmp_path / "cli-args.txt"
    _make_fake_mineru(tmp_path, args_file)
    _make_doclib(tmp_path / "mineru-home", [_win([0], page_count=1)])
    doc = tmp_path / "卷.doc"
    doc.write_bytes(b"%DOC-fake")
    sidecar_mod._tasks["t10"] = {"status": "pending", "progress": 0,
                                 "error": None, "warnings": [], "files": ["卷.doc"]}
    sidecar_mod._run_mineru_sync("t10", [doc])
    task = sidecar_mod._tasks["t10"]
    assert task["status"] == "completed", task.get("error")
    task["_cli_args"] = args_file.read_text(encoding="utf-8").split()
    args = task["_cli_args"]
    assert "--pages" not in args
    assert "--wait" in args  # --wait 对所有输入都需要


def test_pdf_input_keeps_pages_all(sidecar_mod, tmp_path):
    args_file = tmp_path / "cli-args.txt"
    _make_fake_mineru(tmp_path, args_file)
    _make_doclib(tmp_path / "mineru-home", [_win([0], page_count=1)])
    pdf = tmp_path / "卷.pdf"
    pdf.write_bytes(b"%PDF-fake")
    sidecar_mod._tasks["t11"] = {"status": "pending", "progress": 0,
                                 "error": None, "warnings": [], "files": ["卷.pdf"]}
    sidecar_mod._run_mineru_sync("t11", [pdf])
    task = sidecar_mod._tasks["t11"]
    assert task["status"] == "completed", task.get("error")
    task["_cli_args"] = args_file.read_text(encoding="utf-8").split()
    args = task["_cli_args"]
    assert args[args.index("--pages") + 1] == "all"
