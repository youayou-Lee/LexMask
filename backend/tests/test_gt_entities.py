# Issue#56 M1 Task 3 —— 实体三通道 entities.py（正则 + NER + 合并）的离线单测。
# 零网络、零真实案卷数据（全部合成占位符）。
# 类型枚举动态校验：REGEX_CHANNELS 键 ⊆ common_api.TYPE_ID_TO_NAME 值集合
#（读 backend/config/preset_entity_types.json 单一事实源，不硬编码类型名列表）。
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))

from gt import entities, normalize  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 与 test_eval37_run_eval.py 同款按文件路径加载（不污染 sys.path）：
# common_api.TYPE_ID_TO_NAME 读 preset_entity_types.json 单一事实源。
common_api = _load("common_api_under_test", REPO / "eval" / "scripts" / "common_api.py")


def _extract(raw: str):
    """从原始转录文本走 normalize → extract_regex 全链，返回 (实体列表, FaceMap)。"""
    fm = normalize.FaceMap.from_raw(raw)
    return entities.extract_regex(fm.norm, fm), fm


class _FakeNER:
    """离线假 NER 客户端：返回预置 {类型名: [实体串]}，记录调用文本。"""

    def __init__(self, result: dict):
        self.result = result
        self.calls: list[str] = []

    def ner(self, text: str) -> dict:
        self.calls.append(text)
        return self.result


def _ent(text: str, etype: str, n0: int, n1: int, o0: int | None = None,
         o1: int | None = None, origin: str = "regex") -> dict:
    return {"text": text, "type": etype,
            "span_original": [n0 if o0 is None else o0, n1 if o1 is None else o1],
            "span_normalized": [n0, n1], "origin": origin}


# ---- 类型枚举动态校验（全局约束） ---------------------------------------------

def test_regex_channels_exist_in_preset():
    preset_names = set(common_api.TYPE_ID_TO_NAME.values())
    assert set(entities.REGEX_CHANNELS) <= preset_names


def test_regex_patterns_compile():
    for name, pattern in entities.REGEX_CHANNELS.items():
        assert re.compile(pattern), name


# ---- 计划底稿样例（逐字保留） --------------------------------------------------

def test_id_card_boundaries():
    fm = normalize.FaceMap.from_raw("号码110122198110227771后")
    es = entities.extract_regex(fm.norm, fm)
    assert [e["type"] for e in es] == ["身份证号"]


def test_case_number_halfwidth_paren():
    fm = normalize.FaceMap.from_raw("依(2023)桂01民初123号判决")
    es = entities.extract_regex(fm.norm, fm)
    assert any(e["type"] == "案号" and e["text"] == "(2023)桂01民初123号" for e in es)


# ---- 正则通道：每类至少 1 正 1 负 ----------------------------------------------

def test_id_card_17_digits_negative():
    es, _ = _extract("号码11012219811022777后")  # 17 位，差 1 位
    assert [e for e in es if e["type"] == "身份证号"] == []


def test_id_card_with_x_suffix():
    # 17 位数字 + X：身份证（18 字符）压过银行卡（17 位数字，X 前非数字边界）
    es, _ = _extract("证号11012219811022777X，")
    assert [(e["type"], e["text"]) for e in es] == [("身份证号", "11012219811022777X")]


def test_phone_mobile_and_landline_positive():
    es, _ = _extract("电话13800138000，座机010-88888888。")
    assert sorted(e["text"] for e in es if e["type"] == "电话") == \
        ["010-88888888", "13800138000"]


def test_phone_too_short_negative():
    es, _ = _extract("分机0371-123456不完整")  # 座机后仅 6 位
    assert es == []


def test_bank_card_positive():
    es, _ = _extract("卡号6222021234567890尾号")
    # 16 位卡内嵌 "021234567890" 恰好构成电话前缀形态——先长后短占位去重后仅剩银行卡
    assert [(e["type"], e["text"]) for e in es] == [("银行卡号", "6222021234567890")]


def test_bank_card_too_short_negative():
    es, _ = _extract("编号62220212345678共14位")
    # 14 位不满足 \d{15,19}；无数字边界的电话通道可能落在长数字串内部
    #（底稿电话正则无边界约束，逐字保留，交由仲裁处理）——仅断言无银行卡号。
    assert [e for e in es if e["type"] == "银行卡号"] == []


def test_bank_card_digit_boundary_negative():
    es, _ = _extract("长串62220212345678901234结束")  # 20 位连续数字
    assert [e for e in es if e["type"] == "银行卡号"] == []  # 前后非数字：不命中


def test_case_number_fullwidth_paren():
    # 全角括号经归一化转半角后与半角用例同构（底稿：全半角括号都过）
    es, _ = _extract("依（2023）桂01民初123号判决")
    assert any(e["type"] == "案号" and e["text"] == "(2023)桂01民初123号" for e in es)


def test_case_number_negative_no_paren():
    es, _ = _extract("案号2023桂01民初123号无括号")
    assert [e for e in es if e["type"] == "案号"] == []


def test_license_plate_positive():
    es, _ = _extract("车牌沪A12345停靠")
    assert any(e["type"] == "车牌号" and e["text"] == "沪A12345" for e in es)


def test_license_plate_negative_no_letter():
    es, _ = _extract("车牌沪12345无字母")
    assert [e for e in es if e["type"] == "车牌号"] == []


# ---- 通道重叠仲裁：先长后短、特定类型优先、占位去重 -----------------------------

def test_phone_prefix_inside_id_card_excluded():
    # 18 位身份证以 13 开头：电话通道命中前 11 位前缀，被更长更特定的身份证占位排除
    es, _ = _extract("号码130000198001011234后")
    assert [e["type"] for e in es] == ["身份证号"]


def test_equal_span_bank_card_yields_to_id_card():
    # 18 位数字同 span 命中身份证与银行卡：等长时特定类型（身份证）优先
    es, _ = _extract("号码110122198110227771后")
    assert [e["type"] for e in es] == ["身份证号"]


# ---- Entity 形状与双面 span 回填 ------------------------------------------------

def test_entity_shape_and_dual_face_spans():
    raw = "身份证：１１０１２２ １９８１ １０ ２２ ７７７１，末尾"
    fm = normalize.FaceMap.from_raw(raw)
    es = entities.extract_regex(fm.norm, fm)
    assert len(es) == 1
    e = es[0]
    assert set(e) == {"text", "type", "span_original", "span_normalized", "origin"}
    assert e["origin"] == "regex" and e["type"] == "身份证号"
    n0, n1 = e["span_normalized"]
    assert e["text"] == fm.norm[n0:n1] == "110122198110227771"
    o0, o1 = e["span_original"]
    # 原文区间覆盖全角数字与中间被剥离的空白，不含区间外字符
    assert "１１０１２２" in raw[o0:o1] and raw[o0:o1].endswith("７７７１")
    # 双面往返：原文 span 映射回 norm 面与检测面一致
    assert fm.to_normalized(o0, o1) == (n0, n1)
    assert fm.to_original(n0, n1) == (o0, o1)


# ---- NER 通道 -------------------------------------------------------------------

def test_extract_ner_none_and_off():
    fm = normalize.FaceMap.from_raw("张三电话13800138000")
    assert entities.extract_ner(fm.norm, None, fm) == []
    assert entities.extract_ner(fm.norm, entities.NEROff(), fm) == []


def test_extract_ner_locates_spans_and_feeds_norm_face():
    raw = "张三电话13800138000"
    fm = normalize.FaceMap.from_raw(raw)
    ner = _FakeNER({"姓名": ["张三"], "电话": ["13800138000"]})
    es = entities.extract_ner(fm.norm, ner, fm)
    assert ner.calls == [fm.norm]  # 喂给客户端的是 norm 面文本
    by_type = {e["type"]: e for e in es}
    assert by_type["姓名"]["text"] == "张三" and by_type["姓名"]["origin"] == "ner"
    o0, o1 = by_type["姓名"]["span_original"]
    assert raw[o0:o1] == "张三"
    assert fm.to_normalized(o0, o1) == tuple(by_type["姓名"]["span_normalized"])
    assert by_type["电话"]["text"] == "13800138000"


def test_extract_ner_all_occurrences():
    fm = normalize.FaceMap.from_raw("张三与张三")
    es = entities.extract_ner(fm.norm, _FakeNER({"姓名": ["张三"]}), fm)
    assert [e["span_normalized"][0] for e in es] == [0, 3]


def test_extract_ner_unfound_skipped():
    fm = normalize.FaceMap.from_raw("甲乙丙")
    assert entities.extract_ner(fm.norm, _FakeNER({"姓名": ["李四"]}), fm) == []


# ---- 合并：origin 合并与 conflict 标注 ------------------------------------------

def test_merge_same_span_same_type_merges_origin():
    a = [_ent("13800138000", "电话", 2, 13)]
    b = [_ent("13800138000", "电话", 2, 13, origin="ner")]
    merged = entities.merge_entities(a, b)
    assert len(merged) == 1
    assert merged[0]["origin"] == "regex+ner"
    assert merged[0]["span_original"] == [2, 13]
    assert merged[0]["span_normalized"] == [2, 13]


def test_merge_type_conflict_marks_conflict():
    # 同 span 类型冲突：两条均保留并标 conflict（交仲裁）
    a = [_ent("110122198110227771", "身份证号", 0, 18)]
    b = [_ent("110122198110227771", "银行卡号", 0, 18, origin="ner")]
    merged = entities.merge_entities(a, b)
    assert len(merged) == 2
    assert {e["type"] for e in merged} == {"身份证号", "银行卡号"}
    assert all(e["origin"] == "conflict" for e in merged)


def test_merge_disjoint_keeps_all_with_origins():
    a = [_ent("张三", "姓名", 0, 2)]
    b = [_ent("13800138000", "电话", 5, 16, origin="ner")]
    merged = entities.merge_entities(a, b)
    assert len(merged) == 2
    assert merged[0]["span_normalized"] == [0, 2] and merged[0]["origin"] == "regex"
    assert merged[1]["span_normalized"] == [5, 16] and merged[1]["origin"] == "ner"


def test_merge_result_sorted_by_position():
    a = [_ent("乙", "姓名", 5, 6)]
    b = [_ent("甲", "姓名", 0, 1, origin="ner")]
    merged = entities.merge_entities(a, b)
    assert [e["span_normalized"][0] for e in merged] == [0, 5]


# ---- 端到端：正则 + NER + 合并 ---------------------------------------------------

def test_regex_plus_ner_end_to_end():
    raw = "张三，身份证号110122198110227771，电话13800138000。"
    fm = normalize.FaceMap.from_raw(raw)
    regex_es = entities.extract_regex(fm.norm, fm)
    ner_es = entities.extract_ner(
        fm.norm, _FakeNER({"姓名": ["张三"], "身份证号": ["110122198110227771"]}), fm)
    merged = entities.merge_entities(regex_es, ner_es)
    id_es = [e for e in merged if e["text"] == "110122198110227771"]
    assert len(id_es) == 1 and id_es[0]["origin"] == "regex+ner"
    assert any(e["text"] == "张三" and e["origin"] == "ner" for e in merged)
    assert any(e["type"] == "电话" and e["origin"] == "regex" for e in merged)
    # 输出按位置有序，供 Task 5/7 消费
    starts = [e["span_original"][0] for e in merged]
    assert starts == sorted(starts)
