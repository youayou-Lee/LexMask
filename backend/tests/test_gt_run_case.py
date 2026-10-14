# Issue#105 G3 —— run_case_gt.py 一键编排 CLI 的离线单测。
# 零网络：只测纯函数与阶段规划（真实执行靠各阶段自身测试 + 端到端试跑）。
import sys
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import run_case_gt  # noqa: E402


# ---- 阶段规划 ---------------------------------------------------------------

def test_stage_order_and_unknown_rejected():
    assert run_case_gt.STAGES == ("render", "compare", "reduce", "gate", "export")
    with pytest.raises(ValueError):
        run_case_gt.plan_stages("render", "nonsense")
    assert run_case_gt.plan_stages("compare", "export") == \
        ["compare", "reduce", "gate", "export"]


def test_skip_zero_violation_gate():
    """零幻觉门不过 → 强制只重跑 compare 复检（宁可停，不可静默跑歪）。"""
    assert run_case_gt.plan_stages("render", "compare", violations=5) == ["compare"]
    assert run_case_gt.plan_stages("compare", "export", violations=1) == ["compare"]
    assert run_case_gt.plan_stages("reduce", "export", violations=2) == ["compare"]
    # 无违规 → 正常切片
    assert run_case_gt.plan_stages("compare", "export") == \
        ["compare", "reduce", "gate", "export"]


# ---- 流水域签名（内容判据，非页脚判据——xbg 补漏教训） -----------------------

def test_flow_signature_detection():
    assert run_case_gt.is_flow_page("对账单…信用卡还款…财付通") is True
    assert run_case_gt.is_flow_page("EP01000PSDCO42843300133 交易时间") is True
    assert run_case_gt.is_flow_page("讯问笔录：问你出生于1976年9月1日") is False
    assert run_case_gt.is_flow_page("普通笔录正文") is False


# ---- 导出阶段走 export_exam（类型勾选冒烟） ----------------------------------

def test_export_stage_smoke(tmp_path):
    results = tmp_path / "results"
    (results / "[卷]1").mkdir(parents=True)
    (results / "[卷]1" / "p001.json").write_text(json.dumps({
        "stem": "[卷]1", "page_no": 0, "pdf": "[卷]1.pdf",
        "text": "张三支付30000元。",
        "entities": [{"text": "张三", "type": "姓名"},
                     {"text": "30000元", "type": "金额"}],
    }, ensure_ascii=False), encoding="utf-8")
    rows, summary = run_case_gt.export_stage(
        results_dir=results, out_path=tmp_path / "exam.jsonl",
        types={"姓名"}, version="v0")
    assert summary["by_type"] == {"姓名": 1}
    assert (tmp_path / "exam.jsonl").exists()
