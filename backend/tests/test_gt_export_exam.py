# Issue#105 G4 —— 评测考卷导出器 export_exam.py 的离线单测。
# 零网络、零真实案卷数据（全部合成页面）。
#
# 评测集导出形状与 t2 桶 jsonl 同族（page/pdf/page_no/text/entities），
# 关键能力：按类型子集勾选导出（用户拍板：GT 类型全集保留，评测时勾选）。
import sys
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import export_exam  # noqa: E402


def _page(stem="[卷]1", page_no=0, text="张三在2023年6月1日支付30000元。",
          entities=None, pdf="[卷]1.pdf"):
    return {
        "stem": stem, "page_no": page_no, "pdf": pdf, "text": text,
        "entities": entities if entities is not None else [
            {"text": "张三", "type": "姓名"},
            {"text": "2023年6月1日", "type": "日期"},
            {"text": "30000元", "type": "金额"},
        ],
    }


# ---- 基本导出形状 -----------------------------------------------------------

def test_shape_matches_t2_bucket_family():
    out = export_exam.build_exam([_page()])
    assert len(out) == 1
    row = out[0]
    assert row["page"] == "[卷]1.p_p1"
    assert row["pdf"] == "[卷]1.pdf"
    assert row["page_no"] == 0
    assert row["text"].startswith("张三")
    assert {e["type"] for e in row["entities"]} == {"姓名", "日期", "金额"}


def test_write_exam_jsonl_roundtrip(tmp_path):
    rows = export_exam.build_exam([_page(), _page(page_no=1, text="李四。")])
    p = tmp_path / "exam.jsonl"
    export_exam.write_exam_jsonl(rows, p, version="v-test")
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert all(r["gt_version"] == "v-test" for r in lines)
    assert lines[1]["page"] == "[卷]1.p_p2"


# ---- 类型勾选（核心需求） ---------------------------------------------------

def test_types_filter_keeps_selected_only():
    out = export_exam.build_exam([_page()], types={"姓名", "金额"})
    ents = out[0]["entities"]
    assert {e["type"] for e in ents} == {"姓名", "金额"}


def test_types_filter_all_types_returns_everything():
    all_types = {"姓名", "日期", "金额"}
    out = export_exam.build_exam([_page()], types=all_types)
    assert len(out[0]["entities"]) == 3


def test_types_filter_renamed_legacy_types():
    """旧口径类型名（人名/机构名）应映射到 preset 名后参与勾选。"""
    page = _page(entities=[{"text": "张三", "type": "人名"},
                           {"text": "某局", "type": "机构名"},
                           {"text": "30000元", "type": "金额"}])
    out = export_exam.build_exam([page], types={"姓名"})
    assert [e["text"] for e in out[0]["entities"]] == ["张三"]


def test_types_filter_drops_empty_pages():
    """勾选后该页实体为空 → 默认整页剔除（不产空考题）。"""
    page_a = _page(page_no=0, entities=[{"text": "张三", "type": "姓名"}])
    page_b = _page(page_no=1, text="无实体页。", entities=[])
    out = export_exam.build_exam([page_a, page_b], types={"姓名"})
    assert [r["page_no"] for r in out] == [0]
    out_keep = export_exam.build_exam([page_a, page_b], types={"姓名"}, keep_empty=True)
    assert len(out_keep) == 2


# ---- 流水域排除与统计 -------------------------------------------------------

def test_exclude_pages():
    out = export_exam.build_exam(
        [_page(page_no=0), _page(page_no=1)],
        exclude_pages={("[卷]1", 1)})
    assert [r["page_no"] for r in out] == [0]


def test_summary_counts_by_type():
    out = export_exam.build_exam([_page()], types={"姓名", "金额"})
    s = export_exam.exam_summary(out)
    assert s["pages"] == 1 and s["entities"] == 2
    assert s["by_type"] == {"姓名": 1, "金额": 1}


def test_page_id_format_multi_digit():
    out = export_exam.build_exam([_page(page_no=11)])
    assert out[0]["page"] == "[卷]1.p_p12"


# ---- eval-ng 考卷接线（Issue#93 落地后续：产线考卷直进统一评测引擎） ----------

def test_build_evalng_exam_shape():
    doc = export_exam.build_evalng_exam(
        export_exam.build_exam([_page()]), exam_id="exam-v2")
    assert doc["exam_id"] == "exam-v2"
    assert len(doc["items"]) == 1
    it = doc["items"][0]
    assert it["id"] == "[卷]1.p_p1"
    assert it["exam_id"] == "exam-v2"
    assert it["form"] == "entity_table"
    assert it["types"] == sorted(it["gt"])
    assert it["gt"]["姓名"] == ["张三"]
    assert it["text"].startswith("张三")


def test_build_evalng_exam_keeps_duplicate_entities():
    rows = [{"page": "a.p_p1", "pdf": "a.pdf", "page_no": 0, "text": "龙世辉、龙世辉",
             "entities": [{"text": "龙世辉", "type": "姓名"},
                          {"text": "龙世辉", "type": "姓名"}]}]
    doc = export_exam.build_evalng_exam(rows, exam_id="x")
    assert doc["items"][0]["gt"] == {"姓名": ["龙世辉", "龙世辉"]}


def test_build_evalng_exam_bucket_meta_slot():
    doc = export_exam.build_evalng_exam([], exam_id="x",
                                        bucket_meta={"content": "产线考卷"})
    assert doc["buckets"] == {"content": "产线考卷"}
    assert export_exam.build_evalng_exam([], exam_id="x").get("buckets") is None


def test_write_evalng_exam_roundtrip(tmp_path):
    out = tmp_path / "sub" / "exam.json"
    doc = export_exam.build_evalng_exam(
        export_exam.build_exam([_page()]), exam_id="exam-v2")
    export_exam.write_evalng_exam(doc, out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == doc


def test_evalng_exam_loadable_by_evalng_cli(tmp_path, monkeypatch):
    """接线契约：write_evalng_exam 的产物必须能被 eval-ng cli.load_exam_json 吃下。"""
    evalng_dir = REPO / "eval-ng"
    if not evalng_dir.exists():
        pytest.skip("eval-ng 不在检出内")
    # 目录名带连字符，按 cli.py 的自举方式注册包名 evalng
    import sys as _sys
    import types as _types
    if "evalng" not in _sys.modules:
        pkg = _types.ModuleType("evalng")
        pkg.__path__ = [str(evalng_dir)]
        _sys.modules["evalng"] = pkg
    from evalng.engine.protocol import AnswerForm
    from evalng.cli import load_exam_json
    out = tmp_path / "exam.json"
    export_exam.write_evalng_exam(
        export_exam.build_evalng_exam(
            export_exam.build_exam([_page(), _page(page_no=1)]), exam_id="exam-v2"),
        out)
    items = load_exam_json(out, "exam-v2")
    assert len(items) == 2
    assert all(i.form is AnswerForm.ENTITY_TABLE for i in items)
    assert items[0].gt["金额"] == ["30000元"]
