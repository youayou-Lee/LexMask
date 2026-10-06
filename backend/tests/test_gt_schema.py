# Issue#56 M1 Task 6 —— GT schema 校验器 gt_schema.py（validate_pagepack /
# write_gt_jsonl）的离线单测。
# 零网络、零真实案卷数据（全部合成占位符）。
# 类型枚举动态校验：实体 type 合法性读 eval/scripts/common_api.py 的
# TYPE_ID_TO_NAME 值集（backend/config/preset_entity_types.json 单一事实源），
# 不硬编码类型名列表——preset 改名测试不脆。
# 与计划底稿脚手架的差异（机械修正，实现 API 不变，见 task-6-report）：
#   底稿 _min_pack 的 transcript_gt 无 normalized_text——pack 契约携带
#   transcript_gt.normalized_text 且 span_normalized 以归一化面为界，补齐；
#   底稿 return gt_schema.validate_pagepack.__globals__ and {...} 为脚手架
#   残留，去除。
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import compare, gt_schema  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 与 test_gt_entities.py 同款按文件路径加载（不污染 sys.path）：
# 校验器的类型名集必须与 preset 单一事实源逐字一致。
common_api = _load("common_api_under_test_schema",
                   REPO / "eval" / "scripts" / "common_api.py")

VERSION = "gt-test-v1"


def _min_pack():  # 测试夹具：一条合法 pack（各字段最小合法值，合成占位符）
    return {
        "page_id": "t-p0", "page_type": "body",
        "source": {"file_sha256": "0" * 64, "page": 0,
                   "carrier": "text_pdf", "segment": "first"},
        "transcript_gt": {"text": "号码110122198110227771",
                          "normalized_text": "号码110122198110227771",
                          "fidelity": "machine"},
        "entities": [{"text": "110122198110227771", "type": "身份证号",
                      "span_original": [2, 20], "span_normalized": [2, 20],
                      "origin": "regex", "verify": "consistent", "note": None}],
        "adjudications": []}


# ---- 常量（单一事实源） ---------------------------------------------------------

def test_constants_verbatim():
    assert gt_schema.PAGE_TYPES == {"body", "table", "seal_handwriting", "edge"}
    assert gt_schema.ARBITRATION_RULES == {f"R{i}" for i in range(1, 8)}
    assert gt_schema.VERIFY_STATES == {"consistent", "arbitrated",
                                       "user-confirmed", "user-corrected"}


def test_page_types_single_source_of_truth():
    # rider 收编裁定：compare 只 re-export，gt_schema 是唯一事实源
    assert compare.PAGE_TYPES is gt_schema.PAGE_TYPES


# ---- 合法 pack -----------------------------------------------------------------

def test_valid_min_pack_passes():
    assert gt_schema.validate_pagepack(_min_pack()) == []


# ---- 页级字段 ------------------------------------------------------------------

def test_page_id_required_nonempty():
    pack = _min_pack()
    pack["page_id"] = ""
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and "page_id" in errors[0]
    pack["page_id"] = "   "  # 纯空白视同空
    assert gt_schema.validate_pagepack(pack)


def test_page_type_must_be_known():
    pack = _min_pack()
    pack["page_type"] = "empty"  # 底稿旧枚举名，已废
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and "page_type" in errors[0]


def test_source_required_keys():
    for key in ("file_sha256", "page", "carrier", "segment"):
        pack = _min_pack()
        del pack["source"][key]
        errors = gt_schema.validate_pagepack(pack)
        assert len(errors) == 1 and key in errors[0], key


# ---- 实体：type 动态读 preset ---------------------------------------------------

def test_type_must_be_preset_name():
    pack = _min_pack()
    pack["entities"][0]["type"] = "人名"  # preset 里叫「姓名」
    assert any("type" in e for e in gt_schema.validate_pagepack(pack))


def test_preset_names_loaded_dynamically():
    # 名单与 preset 单一事实源逐字一致（不硬编码，改名不脆）
    assert gt_schema.PRESET_TYPE_NAMES == set(common_api.TYPE_ID_TO_NAME.values())


def test_every_preset_name_accepted():
    # 任一 preset 类型名都不该被拒（含非正则通道类型）
    for name in common_api.TYPE_ID_TO_NAME.values():
        pack = _min_pack()
        pack["entities"][0]["type"] = name
        assert not [e for e in gt_schema.validate_pagepack(pack) if ".type" in e], name


def test_non_regex_preset_type_accepted():
    # 校验器读 preset 名集而非 REGEX_CHANNELS 键集：NER 通道类型同样合法
    pack = _min_pack()
    pack["entities"][0] = {"text": "钱明涛", "type": "姓名",
                           "span_original": [0, 3], "span_normalized": [0, 3],
                           "origin": "ner", "verify": "consistent", "note": None}
    assert gt_schema.validate_pagepack(pack) == []


# ---- 实体：双面 span 界 ---------------------------------------------------------

def test_span_original_out_of_bounds():
    pack = _min_pack()
    pack["entities"][0]["span_original"] = [2, 25]  # transcript_gt.text 仅 20 字
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and "span_original" in errors[0]


def test_span_original_start_after_end():
    pack = _min_pack()
    pack["entities"][0]["span_original"] = [10, 5]
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and "span_original" in errors[0]


def test_span_original_negative_start():
    pack = _min_pack()
    pack["entities"][0]["span_original"] = [-1, 20]
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and "span_original" in errors[0]


def test_boundary_span_exact_len_is_valid():
    pack = _min_pack()
    assert pack["entities"][0]["span_original"] == [2, 20]  # 恰抵 text 末端
    assert gt_schema.validate_pagepack(pack) == []


def test_span_normalized_bounded_by_normalized_face():
    # 归一化面比原文短（原文空格被归一化剥除）：span_normalized 以 norm 面长度为界
    pack = _min_pack()
    pack["transcript_gt"]["text"] = "号码 110122198110227771"          # 21 字（含空格）
    pack["transcript_gt"]["normalized_text"] = "号码110122198110227771"  # 20 字
    pack["entities"][0]["span_original"] = [3, 21]
    assert gt_schema.validate_pagepack(pack) == []       # 双面各自合法
    pack["entities"][0]["span_normalized"] = [2, 99]     # 越出归一化面（原文面内）
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and "span_normalized" in errors[0]


# ---- 实体：verify 状态 ----------------------------------------------------------

def test_verify_must_be_known_state():
    pack = _min_pack()
    pack["entities"][0]["verify"] = "auto"
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and "verify" in errors[0]


# ---- 仲裁条目：rule -------------------------------------------------------------

def test_adjudication_rule_must_be_arbitration_rule():
    pack = _min_pack()
    pack["adjudications"] = [{"entity": {}, "rule": "R8", "candidates": {}}]
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and "rule" in errors[0]


def test_adjudication_rules_r1_to_r7_accepted():
    pack = _min_pack()
    pack["adjudications"] = [{"rule": f"R{i}"} for i in range(1, 8)]
    assert gt_schema.validate_pagepack(pack) == []


# ---- 重叠策略：同类型完全同 span 报错；部分重叠 warning 留痕 ---------------------

def test_duplicate_same_type_same_span_is_error():
    pack = _min_pack()
    pack["entities"].append(dict(pack["entities"][0]))
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1
    assert not errors[0].startswith("warning:")
    assert "entities[0]" in errors[0] and "entities[1]" in errors[0]


def test_partial_overlap_same_type_is_warning_not_error():
    pack = _min_pack()
    overlap = dict(pack["entities"][0])
    overlap["span_original"] = [5, 15]
    overlap["span_normalized"] = [5, 15]
    pack["entities"].append(overlap)
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 1 and errors[0].startswith("warning:")
    assert "entities[0]" in errors[0] and "entities[1]" in errors[0]


def test_partial_overlap_different_type_silent():
    pack = _min_pack()
    other = dict(pack["entities"][0])
    other["type"] = "姓名"
    other["text"] = "占位文本"  # 校验器不核对 text/type 一致性（不在契约内）
    other["span_original"] = [5, 15]
    other["span_normalized"] = [5, 15]
    pack["entities"].append(other)
    assert gt_schema.validate_pagepack(pack) == []


def test_three_way_partial_overlap_two_warnings():
    pack = _min_pack()
    base = pack["entities"][0]
    pack["entities"] = [dict(base, span_original=[0, 10], span_normalized=[0, 10]),
                        dict(base, span_original=[5, 15], span_normalized=[5, 15]),
                        dict(base, span_original=[12, 18], span_normalized=[12, 18])]
    errors = gt_schema.validate_pagepack(pack)
    # (e0,e1)、(e1,e2) 部分重叠各一 warning；(e0,e2) 不相交不计
    assert len(errors) == 2 and all(e.startswith("warning:") for e in errors)


# ---- 错误累积 -------------------------------------------------------------------

def test_errors_accumulate_across_fields():
    pack = _min_pack()
    pack["entities"][0]["type"] = "人名"
    pack["entities"][0]["verify"] = "auto"
    pack["adjudications"] = [{"rule": "R0"}]
    errors = gt_schema.validate_pagepack(pack)
    assert len(errors) == 3


# ---- write_gt_jsonl -------------------------------------------------------------

def test_write_gt_jsonl_one_line_per_page_with_version(tmp_path):
    packs = [_min_pack(), _min_pack()]
    packs[1]["page_id"] = "t-p1"
    out = tmp_path / "gt.jsonl"
    gt_schema.write_gt_jsonl(packs, out, VERSION)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert [r["page_id"] for r in rows] == ["t-p0", "t-p1"]
    assert all(r["gt_version"] == VERSION for r in rows)
    assert all(r["entities"][0]["type"] == "身份证号" for r in rows)  # 内容原样
    # 注入发生在写出副本上，入参不被修改
    assert all("gt_version" not in p for p in packs)


def test_write_gt_jsonl_keeps_chinese_unescaped(tmp_path):
    out = tmp_path / "gt.jsonl"
    gt_schema.write_gt_jsonl([_min_pack()], out, VERSION)
    assert "身份证号" in out.read_text(encoding="utf-8")  # ensure_ascii=False


def test_write_gt_jsonl_stamps_adjudications_at(tmp_path):
    # spec §4：adjudications 携带 "at" = 定稿日；pack 定稿前不落日期（校验不要求，
    # 注入只发生在写出副本上），写出后每条仲裁条目带写入当日 ISO 日期
    pack = _min_pack()
    pack["adjudications"] = [{"rule": "R2", "entity": {}, "candidates": {}}]
    assert all("at" not in adj for adj in pack["adjudications"])  # 定稿前无 at
    assert gt_schema.validate_pagepack(pack) == []                # 校验不要求 at
    out = tmp_path / "gt.jsonl"
    gt_schema.write_gt_jsonl([pack], out, VERSION)
    row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert len(row["adjudications"]) == 1
    at = row["adjudications"][0]["at"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", at)      # ISO 日期
    assert at == date.today().isoformat()              # = 写出（定稿）当日
    assert all("at" not in adj for adj in pack["adjudications"])  # 入参不被修改


def test_write_gt_jsonl_refuses_invalid_pack(tmp_path):
    pack = _min_pack()
    pack["entities"][0]["type"] = "人名"
    out = tmp_path / "gt.jsonl"
    with pytest.raises(ValueError) as ei:
        gt_schema.write_gt_jsonl([pack], out, VERSION)
    assert "type" in str(ei.value) and "packs[0]" in str(ei.value)
    assert not out.exists()  # 校验先行：坏包不落盘


def test_write_gt_jsonl_refuses_if_any_pack_invalid(tmp_path):
    good, bad = _min_pack(), _min_pack()
    bad["page_id"] = "t-bad"
    bad["entities"][0]["verify"] = "auto"
    out = tmp_path / "gt.jsonl"
    with pytest.raises(ValueError) as ei:
        gt_schema.write_gt_jsonl([good, bad], out, VERSION)
    assert "packs[1]" in str(ei.value)
    assert not out.exists()
