# Issue#56 P0 —— 三分法闸门 gate.py 的离线单测。
# 零网络、零真实案卷数据（全部合成读数）。
#
# 这是整套设计的安全阀：Agent 说"这两处是同一个东西的不同写法"，程序必须能验。
# 本文件锁定四条不可退让的性质：
#   1. 判据是差异的**性质**（域），不是差异的**大小**；
#   2. 裁决永不使用模型自信度（VERDICTS 里没有"低信心放行"这条路）；
#   3. 证据闸门只接受**异源多数**佐证（同源只计一次，否则伪造"多数"）；
#   4. 口径类判定与档位无关，档位只改"值差怎么办"。
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import gate  # noqa: E402


# ---- 配置完整性 ------------------------------------------------------------

@pytest.mark.parametrize("level", gate.STRICTNESS_LEVELS)
def test_every_situation_configured(level):
    cfg = gate.gate_config(level)
    missing = [s for s in gate.SITUATIONS if s not in cfg]
    assert missing == [], f"{level} 缺策略：{missing}"


@pytest.mark.parametrize("level", gate.STRICTNESS_LEVELS)
def test_every_action_is_legal(level):
    legal = {"pass", "pass_suspect", "human", "needs_soft"}
    cfg = gate.gate_config(level)
    for s in gate.SITUATIONS:
        assert cfg[s] in legal, f"{level}.{s} = {cfg[s]!r} 不是合法动作"


def test_verdicts_have_no_confidence_path():
    """放行只有三条出路，且都不依赖模型自评。"""
    assert set(gate.VERDICTS) == {"auto_pass", "needs_soft", "human_pending"}


@pytest.mark.parametrize("level", gate.STRICTNESS_LEVELS)
def test_no_situation_silently_passes(level):
    """除归一化等价以外，没有任何情形在交付档是"默默放行"。"""
    cfg = gate.gate_config(level)
    assert cfg["normalized_equal"] == "pass"
    if level == "S2-delivery":
        suspicious = [s for s in gate.SITUATIONS
                      if cfg[s] == "pass_suspect"]
        assert suspicious == [], f"交付档不得有 suspect 放行：{suspicious}"


def test_gate_config_returns_a_copy():
    cfg = gate.gate_config("S1-internal")
    cfg["identifier_mismatch"] = "pass"
    assert gate.gate_config("S1-internal")["identifier_mismatch"] == "human"


def test_unknown_level_rejected():
    with pytest.raises(ValueError):
        gate.gate_verdict("姓名", "张三", "张三丰", level="S9-whatever")


def test_unknown_entity_type_rejected():
    with pytest.raises(KeyError):
        gate.gate_verdict("不存在的类型", "a", "b")


# ---- 口径层：与档位无关 ----------------------------------------------------

@pytest.mark.parametrize("level", gate.STRICTNESS_LEVELS)
def test_normalized_equal_passes_in_every_level(level):
    """L0.5 归一回一是免审率的主要来源，任何档位都必须放行。"""
    r = gate.gate_verdict("姓名", "张 三", "张三", level=level)
    assert r["verdict"] == "auto_pass"
    assert r["situation"] == "normalized_equal"
    assert r["suspect"] is False


@pytest.mark.parametrize("level", gate.STRICTNESS_LEVELS)
def test_one_side_empty_is_always_human(level):
    """缺读数不是"差异"，任何档位都不许自动放行。"""
    r = gate.gate_verdict("姓名", "", "张三", level=level)
    assert r["verdict"] == "human_pending"
    assert r["situation"] == "one_side_empty"


# ---- 强标识符：逐位相等，不做任何模糊判断 ----------------------------------

@pytest.mark.parametrize("level", ["S1-internal", "S2-delivery"])
def test_identifier_value_difference_never_auto_passes(level):
    """AC7：强标识符值差在内部/交付档 100% 不自动放行。"""
    r = gate.gate_verdict("身份证号", "110101199001011234",
                          "110101199001011284", level=level)
    assert r["verdict"] == "human_pending"
    assert r["domain"] == "identifier"


def test_identifier_difference_is_flagged_not_hidden_in_s0():
    """S0 为了跑通会放行值差，但必须打上 suspect 且永不进正式 GT（AC9）。"""
    r = gate.gate_verdict("身份证号", "110101199001011234",
                          "110101199001011284", level="S0-trial")
    assert r["verdict"] == "auto_pass"
    assert r["suspect"] is True, "S0 的值差放行必须显式标 suspect"


def test_similar_strings_are_not_equivalent():
    """94% 相似度的两个身份证号是值差——整体相似度在这里没有发言权。"""
    a, b = "110101199001011234", "110101199001011284"
    assert gate.gate_verdict("身份证号", a, b, level="S1-internal")["verdict"] \
        == "human_pending"


# ---- 日期粒度差：容忍但要证据 ----------------------------------------------

def _date_dispute(canonical="2023年6月15日", sources=("mineru", "paddle_vl")):
    soft = {"dispute_id": "d1", "diff_class": "unclear",
            "canonical_form": canonical}
    transcripts = [(s, f"被告人于{canonical}签署上述材料。") for s in sources]
    return soft, transcripts


def test_date_granularity_gap_with_evidence_can_auto_pass():
    soft, transcripts = _date_dispute()
    r = gate.gate_verdict("日期", "2023年", "2023年6月15日", soft=soft,
                          level="S1-internal", transcripts=transcripts)
    assert r["verdict"] == "auto_pass"
    assert r["soft_verified"] is True
    assert r["situation"] == "datetime_granularity_gap_verified"


def test_date_granularity_gap_without_evidence_goes_to_human():
    """§7.3：可能是 OCR 截断，也可能是凭空补全——没证据就不许放行。"""
    soft, transcripts = _date_dispute(sources=("mineru",))
    r = gate.gate_verdict("日期", "2023年", "2023年6月15日", soft=soft,
                          level="S1-internal", transcripts=transcripts)
    assert r["verdict"] == "human_pending"
    assert r["soft_verified"] is False
    assert r["anomaly"] == "soft_unverified"


def test_date_granularity_gap_without_any_transcripts_is_human():
    soft, _ = _date_dispute()
    r = gate.gate_verdict("日期", "2023年", "2023年6月15日", soft=soft,
                          level="S1-internal")
    assert r["verdict"] == "human_pending"


def test_same_source_counted_once():
    """同一个 source_id 出现多次只算一个佐证。

    本地 vl-md 与云 VL 都是 PaddleOCR-VL 1.6，若按调用次数计数就会伪造"多数"。
    """
    soft, _ = _date_dispute()
    repeated = [
        ("mineru", "被告人于2023年6月15日签署上述材料。"),
        ("mineru", "被告人于2023年6月15日签署上述材料。"),
        ("paddle_vl", "被告人于2023年6月15日签署上述材料。"),
    ]
    r = gate.gate_verdict("日期", "2023年", "2023年6月15日", soft=soft,
                          level="S1-internal", transcripts=repeated)
    assert r["evidence"]["evidence_gate"]["corroborating_sources"] == 2
    assert r["verdict"] == "auto_pass"

    # 只有一个异源通道（哪怕重复给三遍）→ 佐证不足 → 人审
    single = [("paddle_vl", "被告人于2023年6月15日签署上述材料。")] * 3
    r2 = gate.gate_verdict("日期", "2023年", "2023年6月15日", soft=soft,
                           level="S1-internal", transcripts=single)
    assert r2["evidence"]["evidence_gate"]["corroborating_sources"] == 1
    assert r2["verdict"] == "human_pending"


def test_gate_records_required_source_count():
    soft, transcripts = _date_dispute()
    r = gate.gate_verdict("日期", "2023年", "2023年6月15日", soft=soft,
                          level="S1-internal", transcripts=transcripts)
    assert r["evidence"]["evidence_gate"]["required"] == gate.MIN_CORROBORATING_SOURCES


def test_date_value_conflict_is_always_human():
    soft, transcripts = _date_dispute()
    r = gate.gate_verdict("日期", "2023年6月15日", "2023年6月16日", soft=soft,
                          level="S1-internal", transcripts=transcripts)
    assert r["verdict"] == "human_pending"
    assert r["situation"] == "datetime_value_mismatch"
    assert r["domain"] == "datetime"


# ---- text/name：软层分诊 + 证据闸门 ----------------------------------------

def test_name_needs_soft_when_no_soft_output_yet():
    r = gate.gate_verdict("姓名", "李四", "李 四 ", level="S1-internal")
    # 归一化后其实相等，所以走 normalized_equal；换一组真不同的
    assert r["verdict"] == "auto_pass"
    r2 = gate.gate_verdict("姓名", "李四", "李肆", level="S1-internal")
    assert r2["verdict"] == "needs_soft"
    assert r2["situation"] == "no_soft_available"
    assert gate.requires_soft(r2) is True


def test_name_soft_absent_in_s0_passes_suspect():
    r = gate.gate_verdict("姓名", "李四", "李肆", level="S0-trial")
    assert r["verdict"] == "auto_pass" and r["suspect"] is True


def test_name_morphological_outlier_always_needs_evidence_even_when_normalized_equal():
    """归一化层验证不了近形字——归一能过的话第 0 步早就放行了。

    所以 name 形态差的价值在**分诊与证据**（让人审更快），不在提高自动放行率。
    """
    soft = {"dispute_id": "d2", "diff_class": "morphological", "canonical_form": "李四"}
    r = gate.gate_verdict("姓名", "李四", "李四", soft=soft, level="S1-internal")
    assert r["situation"] == "normalized_equal"   # 归一化层先兜住


def test_name_morphological_with_evidence_auto_passes():
    soft = {"dispute_id": "d3", "diff_class": "morphological", "canonical_form": "张三"}
    transcripts = [("mineru", "犯罪嫌疑人张三供述如下。"),
                   ("paddle_vl", "犯罪嫌疑人张三供述如下。")]
    r = gate.gate_verdict("姓名", "张三", "张叁", soft=soft, level="S1-internal",
                          transcripts=transcripts)
    assert r["verdict"] == "auto_pass"
    assert r["situation"] == "text_name_morphological_verified"


def test_name_morphological_without_evidence_is_human_in_s1():
    soft = {"dispute_id": "d4", "diff_class": "morphological", "canonical_form": "张三"}
    r = gate.gate_verdict("姓名", "张三", "张叁", soft=soft, level="S1-internal",
                          transcripts=[("mineru", "没有这个名字。")])
    assert r["verdict"] == "human_pending"
    assert r["anomaly"] == "soft_unverified"


def test_soft_missing_canonical_form_cannot_pass():
    soft = {"dispute_id": "d5", "diff_class": "morphological"}
    transcripts = [("mineru", "张三"), ("paddle_vl", "张三")]
    r = gate.gate_verdict("姓名", "张三", "张叁", soft=soft, level="S1-internal",
                          transcripts=transcripts)
    assert r["verdict"] == "human_pending"


def test_name_value_mismatch_never_passes_in_s1():
    soft = {"dispute_id": "d6", "diff_class": "value", "canonical_form": "张三"}
    r = gate.gate_verdict("姓名", "张三", "张三丰", soft=soft, level="S1-internal")
    assert r["verdict"] == "human_pending"
    assert r["situation"] == "text_value_mismatch"


def test_name_missing_and_unclear_go_to_human():
    for cls, situation in (("missing", "text_missing"), ("unclear", "text_unclear")):
        soft = {"dispute_id": "d7", "diff_class": cls}
        r = gate.gate_verdict("姓名", "张三", "李四", soft=soft, level="S0-trial")
        assert r["verdict"] == "human_pending"
        assert r["situation"] == situation


# ---- text/enum -------------------------------------------------------------

def test_enum_mismatch():
    r = gate.gate_verdict("性别", "男", "女", level="S1-internal")
    assert r["verdict"] == "human_pending"
    assert r["situation"] == "text_enum_mismatch"
    r0 = gate.gate_verdict("性别", "男", "女", level="S0-trial")
    assert r0["verdict"] == "auto_pass" and r0["suspect"] is True


# ---- text/passage：重叠率 + 数字守卫，不按逐字 ------------------------------

_BASE = "被告人张三于2023年5月1日在深圳市南山区实施盗窃行为"


def test_passage_punctuation_only_difference_can_pass_in_s1():
    r = gate.gate_verdict("法律记录", _BASE, _BASE.replace("南山区", "南山区,"),
                          level="S1-internal")
    assert r["verdict"] == "auto_pass", r["reason"]
    assert r["situation"] == "text_passage_equivalent"
    assert r["evidence"]["passage"]["cjk_free_diff"] is True


def test_passage_embedded_digit_change_is_caught():
    """长文本整体重叠率很高，会把「内嵌身份证号差一位」稀释掉——数字必须单独比。"""
    a = "被告人张三，身份证号110101199001011234，住深圳市南山区"
    b = "被告人张三，身份证号110101199001011284，住深圳市南山区"
    r = gate.gate_verdict("法律记录", a, b, level="S1-internal")
    assert r["verdict"] == "human_pending"
    assert r["situation"] == "text_passage_digit_mismatch"
    assert r["evidence"]["passage"]["digit_runs_equal"] is False


def test_passage_cjk_difference_is_not_auto_passed():
    r = gate.gate_verdict("法律记录", _BASE, _BASE.replace("南山区", "福田区"),
                          level="S1-internal")
    assert r["verdict"] == "human_pending"
    assert r["evidence"]["passage"]["cjk_free_diff"] is False


def test_delivery_level_never_passes_fuzzy_passages():
    """S2 比 S1 唯一更严之处：模糊重叠在交付档必须人确认。"""
    r = gate.gate_verdict("法律记录", _BASE, _BASE.replace("南山区", "南山区,"),
                          level="S2-delivery")
    assert r["verdict"] == "human_pending"
    assert r["situation"] == "text_passage_review"


def test_passage_records_overlap_and_threshold():
    r = gate.gate_verdict("法律记录", _BASE, _BASE.replace("南山区", "福田区"),
                          level="S1-internal")
    p = r["evidence"]["passage"]
    assert 0.0 <= p["overlap"] <= 1.0
    assert p["threshold"] == 0.85
    assert p["regions"], "差异区必须记下来，否则人审无从下手"


# ---- 档位只改"值差怎么办" --------------------------------------------------

@pytest.mark.parametrize("entity_type,a,b", [
    ("身份证号", "110101199001011234", "110101199001011284"),
    ("日期", "2023年6月15日", "2023年6月16日"),
    ("金额", "10000元", "100000元"),
    ("性别", "男", "女"),
])
def test_level_only_changes_value_difference_policy(entity_type, a, b):
    s0 = gate.gate_verdict(entity_type, a, b, level="S0-trial")
    s1 = gate.gate_verdict(entity_type, a, b, level="S1-internal")
    s2 = gate.gate_verdict(entity_type, a, b, level="S2-delivery")
    assert s0["verdict"] == "auto_pass" and s0["suspect"] is True
    assert s1["verdict"] == "human_pending"
    assert s2["verdict"] == "human_pending"


def test_name_without_soft_is_a_flow_signal_not_a_value_verdict():
    """text/name 没有软层输出时，内部/交付档的结论是"去调软层"（流程信号），
    不是"这处读数是错的"（值判定）。两者混为一谈会把流程问题记成质量问题。"""
    s0 = gate.gate_verdict("姓名", "张三", "张三丰", level="S0-trial")
    s1 = gate.gate_verdict("姓名", "张三", "张三丰", level="S1-internal")
    assert s0["verdict"] == "auto_pass" and s0["suspect"] is True
    assert s1["verdict"] == "needs_soft"
    assert gate.requires_soft(s1) is True
    # 而一旦软层判为值差，就变成不可放行的质量问题
    soft = {"dispute_id": "d8", "diff_class": "value"}
    assert gate.gate_verdict("姓名", "张三", "张三丰", soft=soft,
                             level="S0-trial")["verdict"] == "auto_pass"
    assert gate.gate_verdict("姓名", "张三", "张三丰", soft=soft,
                             level="S0-trial")["suspect"] is True


def test_render_hint_truncates_long_text():
    assert gate.render_hint("张三") == "张三"
    long = gate.render_hint("被" * 100)
    assert len(long) <= 25 and long.endswith("…")


def test_quantity_gate_uses_value_norm():
    r = gate.gate_verdict("金额", "3万5千元", "35000元", level="S1-internal")
    assert r["verdict"] == "auto_pass" and r["situation"] == "normalized_equal"
    r2 = gate.gate_verdict("金额", "10000元", "100000元", level="S1-internal")
    assert r2["verdict"] == "human_pending" and r2["situation"] == "quantity_mismatch"