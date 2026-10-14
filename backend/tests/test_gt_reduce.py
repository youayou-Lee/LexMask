# Issue#56 P1 —— L0.5 归一回一 reduce.py 的离线单测。
# 零网络、零真实案卷数据（全部合成候选）。
#
# Step 5（方案 §5）：两侧候选各自过 normalize() + 语义归一（date_norm/value_norm），
# 归一后相等 → normalized_closed，**不调模型**；归一抛异常 → 转 disputed（不吞异常）。
# AC2：同输入重跑结论逐位一致。
import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import dispute as gt_dispute  # noqa: E402
from gt import reduce as gt_reduce  # noqa: E402


def _d(entity_type, a, b, **kw):
    return gt_dispute.new_dispute(
        page_id=kw.pop("page_id", "sha256:abc:1"), rule=kw.pop("rule", "R2"),
        entity_type=entity_type, key=kw.pop("key", a),
        candidates={"a": [a], "b": [b], "md": []}, **kw)


# ---- 文本域：形态差关闭 -----------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("张三", "张 三"),            # 空格
    ("清远市公安局", "清远市 公安局"),
])
def test_text_form_diff_closes(a, b):
    out = gt_reduce.reduce_dispute(_d("姓名", a, b))
    assert out["verdict"] == "normalized_closed"


# ---- identifier：归一后逐位相等才关，不等留待闸门 ---------------------------

def test_identifier_spaces_close():
    out = gt_reduce.reduce_dispute(
        _d("身份证号", "441801199001011234", "441801 199001011234"))
    assert out["verdict"] == "normalized_closed"


def test_identifier_value_diff_stays():
    out = gt_reduce.reduce_dispute(
        _d("身份证号", "110101199001011234", "110101199001011284"))
    assert out["verdict"] == "disputed"
    assert "逐位" in out["reason"]


# ---- datetime：严格相等关闭；粒度差**不**自动关（GT 生产必过证据闸门） -------

def test_datetime_many_forms_close():
    out = gt_reduce.reduce_dispute(
        _d("日期", "二〇二三年六月十五日", "2023-06-15"))
    assert out["verdict"] == "normalized_closed"
    assert out["method"] == "date_norm"


def test_datetime_with_time_close():
    out = gt_reduce.reduce_dispute(
        _d("时间", "2023年6月15日 10时30分", "2023年6月15日10:30"))
    assert out["verdict"] == "normalized_closed"


def test_datetime_granularity_gap_not_closed():
    """§7.3：粒度差算同一实体 ≠ GT 生产可自动放行——可能是 OCR 截断或凭空补全。"""
    out = gt_reduce.reduce_dispute(_d("日期", "2023年", "2023年6月15日"))
    assert out["verdict"] == "disputed"
    assert "粒度" in out["reason"]


def test_datetime_conflict_stays():
    out = gt_reduce.reduce_dispute(_d("日期", "2023年6月15日", "2023年6月16日"))
    assert out["verdict"] == "disputed"


def test_datetime_unparsable_stays():
    out = gt_reduce.reduce_dispute(_d("日期", "近日", "2023年6月15日"))
    assert out["verdict"] == "disputed"


# ---- quantity：1万元 == 10000元 关；值差不关 --------------------------------

def test_amount_unit_forms_close():
    out = gt_reduce.reduce_dispute(_d("金额", "1万元", "10000元"))
    assert out["verdict"] == "normalized_closed"
    assert out["method"] == "value_norm"


def test_amount_value_diff_stays():
    out = gt_reduce.reduce_dispute(_d("金额", "10000元", "100000元"))
    assert out["verdict"] == "disputed"


def test_quantity_age_close():
    out = gt_reduce.reduce_dispute(_d("年龄", "三十五岁", "35岁"))
    assert out["verdict"] == "normalized_closed"


# ---- 未知类型（G3 漂移可见）与缺边 -----------------------------------------

def test_unknown_entity_type_stays_visible():
    """「机构名」不在 preset 68 类——正是 xbg-clean2 的 118 条漂移。
    保守不关，且 reason 可见可计数，不静默。"""
    out = gt_reduce.reduce_dispute(_d("机构名", "清远市公安局", "清远市 公安局"))
    assert out["verdict"] == "disputed"
    assert "未登记" in out["reason"]


def test_missing_side_stays():
    d = _d("姓名", "张三", "张三")
    d["candidates"]["b"] = []
    out = gt_reduce.reduce_dispute(d)
    assert out["verdict"] == "disputed"
    assert "空" in out["reason"]


# ---- 异常路径：转 disputed 不吞异常，批不中断 ------------------------------

def test_normalize_exception_becomes_disputed():
    d = _d("姓名", "张三", "张三")
    d["candidates"]["b"] = [{"not": "a str"}]  # normalize_text 会 KeyError
    out = gt_reduce.reduce_dispute(d)
    assert out["verdict"] == "disputed"
    assert "归一化异常" in out["reason"]
    assert "KeyError" in out["reason"]


def test_exception_in_one_dispute_does_not_kill_batch():
    bad = _d("姓名", "张三", {"x": 1})
    good = _d("姓名", "李 四", "李四", page_id="sha256:abc:2")
    outs = gt_reduce.reduce_disputes([bad, good])
    assert outs[0]["verdict"] == "disputed"
    assert outs[1]["verdict"] == "normalized_closed"


# ---- AC2 幂等：同输入重跑结论逐位一致 ---------------------------------------

def test_idempotent_bitwise(tmp_path):
    batch = [
        _d("姓名", "张 三", "张三"),
        _d("日期", "二〇二三年六月十五日", "2023-06-15", page_id="sha256:abc:2"),
        _d("金额", "1万元", "10000元", page_id="sha256:abc:3"),
        _d("身份证号", "110101199001011234", "110101199001011284", page_id="sha256:abc:4"),
        _d("日期", "2023年", "2023年6月15日", page_id="sha256:abc:5"),
        _d("机构名", "某局", "某 局", page_id="sha256:abc:6"),
    ]
    r1 = gt_reduce.reduce_disputes(copy.deepcopy(batch))
    r2 = gt_reduce.reduce_disputes(copy.deepcopy(batch))
    assert json.dumps(r1, ensure_ascii=False, sort_keys=True) == \
           json.dumps(r2, ensure_ascii=False, sort_keys=True)


def test_reduce_transitions_queue(tmp_path):
    d1 = _d("姓名", "张 三", "张三")
    d2 = _d("金额", "10000元", "100000元", page_id="sha256:abc:2")
    q = gt_dispute.DisputeQueue([d1, d2])
    gt_reduce.reduce_disputes([d1, d2], queue=q)
    assert q.get(d1["dispute_id"])["status"] == "normalized_closed"
    assert q.get(d2["dispute_id"])["status"] == "pending"  # 留给软层


# ---- 免审率对比：语义归一 vs 字符级 exact 基线 ------------------------------

def _synthetic_batch():
    """同一批合成争议（覆盖各域 × 形态差/值差），用于两种尺子的对比。"""
    rows = [
        # 形态差——两种尺子都应关（文本空格/全半角）
        ("姓名", "张三", "张 三", True),
        ("地址", "清远市清城区", "清远市 清城区", True),
        ("机关单位", "清远市公安局", "清远市公安局", True),
        # 语义形态差——只有语义归一关（日期/金额写法）
        ("日期", "二〇二三年六月十五日", "2023-06-15", True),
        ("日期", "2023年6月15日", "20230615", True),
        ("日期", "2023年6月15日 10时30分", "2023年6月15日10:30", True),
        ("金额", "1万元", "10000元", True),
        ("金额", "人民币叁仟元", "3000元", True),
        ("年龄", "三十五岁", "35岁", True),
        # 粒度差——语义归一**故意不关**（必过证据闸门）
        ("日期", "2023年", "2023年6月15日", False),
        # 值差——两种尺子都不关
        ("身份证号", "110101199001011234", "110101199001011284", False),
        ("金额", "10000元", "100000元", False),
        ("日期", "2023年6月15日", "2023年6月16日", False),
        ("姓名", "张三", "张三丰", False),
        # 漂移类型——保守不关
        ("机构名", "清远市公安局", "清远市 公安局", False),
    ]
    return [_d(t, a, b, page_id=f"sha256:abc:{i}") for i, (t, a, b, _) in enumerate(rows)]


def test_review_exempt_rate_beats_baseline(capsys):
    """语义归一的免审率必须 ≥ 字符级 exact 基线，差额来自日期/数值域。
    数字打印到 stdout 供排期引用。"""
    batch = _synthetic_batch()
    report = gt_reduce.reduce_report(batch)
    assert report["semantic_closed_ratio"] >= report["baseline_closed_ratio"]
    # 意图断言：那批「应当语义关闭」的样本必须全关
    assert report["semantic_closed_ratio"] == pytest.approx(9 / 15)
    # 基线 4/15：第 4 个是「机构名」漂移行——字符级 exact 会把未登记类型也放行
    # （G3 危害实证），语义归一拒关并留 reason。免审率对比即 9/15 vs 4/15。
    assert report["baseline_closed_ratio"] == pytest.approx(4 / 15)
    print(f"\n免审率：语义归一 {report['semantic_closed_ratio']:.0%} "
          f"vs 字符级 exact 基线 {report['baseline_closed_ratio']:.0%}")


def test_report_counts_consistent():
    batch = _synthetic_batch()
    report = gt_reduce.reduce_report(batch)
    assert report["total"] == len(batch)
    assert (report["semantic_closed"] + report["disputed"]
            == report["total"])
