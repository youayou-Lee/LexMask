# Issue#56 P0 —— 实体类型四类分域 type_classes.py 的离线单测。
# 零网络、零真实案卷数据（全部使用 preset 里的公开类型名）。
#
# 本文件最重要的一条断言是 test_preset_fully_classified：它拿项目**自己的**
# preset 加载器（gt_schema.PRESET_TYPE_NAMES）去对分域表，两侧都必须为空。
# preset 一旦新增类型而分域表没跟上，这条会红——这正是 P0 要堵的漂移口。
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
from gt import gt_schema, type_classes  # noqa: E402
from gt import value_norm  # noqa: E402


# ---- 与 preset 的一致性（漂移门） -----------------------------------------

def test_preset_fully_classified():
    """preset 68 类必须全部落到某个域，且分域表不得有 preset 里没有的类型。"""
    report = type_classes.coverage_report(gt_schema.PRESET_TYPE_NAMES)
    assert report["unclassified"] == [], f"preset 有类型未分域：{report['unclassified']}"
    assert report["unknown"] == [], f"分域表有 preset 里不存在的类型：{report['unknown']}"


def test_map_size_matches_preset():
    assert len(type_classes.TYPE_DOMAIN_MAP) == len(gt_schema.PRESET_TYPE_NAMES)


def test_no_name_falls_into_two_domains():
    """同名落两域会让 classify 结果依赖字典顺序——构建期就该炸。"""
    seen: dict[str, str] = {}
    for name, (domain, _sub) in type_classes.TYPE_DOMAIN_MAP.items():
        assert name not in seen, f"{name} 同时落在 {seen[name]} 与 {domain}"
        seen[name] = domain


def test_quantity_domain_matches_value_norm_kinds():
    """quantity 域与 value_norm.VALUE_KIND_BY_TYPE 必须一一对应（互为子集）。

    两个模块各有一份「哪些类型按数值比」的知识，漂移了就有一边悄悄失效。
    """
    quantity_names = {n for n, (d, _s) in type_classes.TYPE_DOMAIN_MAP.items()
                      if d == "quantity"}
    assert quantity_names == set(value_norm.VALUE_KIND_BY_TYPE)


# ---- 分域结果 --------------------------------------------------------------

@pytest.mark.parametrize("name,domain,sub", [
    ("身份证号", "identifier", None),
    ("银行卡号", "identifier", None),
    ("案号", "identifier", None),
    ("日期", "datetime", None),
    ("出生日期", "datetime", None),
    ("金额", "quantity", None),
    ("年龄", "quantity", None),
    ("性别", "text", "enum"),
    ("民族", "text", "enum"),
    ("姓名", "text", "name"),
    ("机构名称", "text", "name"),
    ("地址", "text", "name"),
    ("法律记录", "text", "passage"),
    ("健康信息", "text", "passage"),
])
def test_classify_known(name, domain, sub):
    assert type_classes.classify(name) == (domain, sub)


def test_classify_unknown_raises_with_hint():
    with pytest.raises(KeyError) as exc:
        type_classes.classify("不存在的类型")
    assert "type_classes.TYPE_DOMAIN_MAP" in str(exc.value)


def test_domain_of_and_is_identifier():
    assert type_classes.domain_of("姓名") == "text"
    assert type_classes.is_identifier("车牌号") is True
    assert type_classes.is_identifier("姓名") is False


def test_location_takes_strict_branch():
    """定位位置 既可能是经纬度（强标识符形态）也可能是文本描述——按取严归 identifier。"""
    assert type_classes.classify("定位位置") == ("identifier", None)


def test_all_domains_and_subdomains_populated():
    domains = {d for d, _s in type_classes.TYPE_DOMAIN_MAP.values()}
    assert domains == set(type_classes.DOMAINS)
    subs = {s for _d, s in type_classes.TYPE_DOMAIN_MAP.values() if s}
    assert subs == set(type_classes.TEXT_SUBDOMAINS)


# ---- 严格度 ----------------------------------------------------------------

def test_strictness_rank_follows_declared_order():
    ranks = [type_classes.strictness_rank(n)
             for n in ("身份证号", "金额", "日期", "姓名")]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks), "四个域必须各有不同严格度"


def test_most_strict_picks_strictest():
    assert type_classes.most_strict(["姓名", "身份证号"]) == "身份证号"
    assert type_classes.most_strict(["姓名", "日期", "金额"]) == "金额"
    # 也接受域名入参
    assert type_classes.most_strict(["text", "datetime"]) == "datetime"
    assert type_classes.most_strict(["姓名"]) == "姓名"


def test_most_strict_rejects_empty():
    with pytest.raises(ValueError):
        type_classes.most_strict([])


def test_most_strict_rejects_unknown():
    with pytest.raises(KeyError):
        type_classes.most_strict(["姓名", "不存在的类型"])


# ---- coverage_report 的差集语义 -------------------------------------------

def test_coverage_report_both_directions():
    # 方向一：列表里的类型没登记进分域表 → unclassified
    r = type_classes.coverage_report(["姓名", "不存在的类型"])
    assert r["unclassified"] == ["不存在的类型"]
    # 方向二：分域表里有、但 preset 没给 → unknown（preset 精简时能发现）
    r2 = type_classes.coverage_report(["姓名"])
    assert r2["unclassified"] == []
    assert len(r2["unknown"]) == len(type_classes.TYPE_DOMAIN_MAP) - 1

    r3 = type_classes.coverage_report(["姓名", "日期"])
    assert r3["unclassified"] == [] and "日期" not in r3["unknown"]