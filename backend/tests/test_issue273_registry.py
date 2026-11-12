"""Issue#273 Task 6：误判样本 registry loader + 种子登记测试。

需求书测试逐字保留。唯一偏差：``get_routing_thresholds`` 的实际落点在
``app.core.routing_thresholds``（Task 1 实现），需求书写的
``app.services.page_routing.routing_thresholds`` 路径不存在，故 import 按实际代码修正。
"""

import json

import pytest

from app.core.routing_thresholds import get_routing_thresholds
from app.services.page_routing import registry
from app.services.page_routing.judge import judge_page
from app.services.page_routing.signals import collect_l1


def test_registry_loader_shape(tmp_path, monkeypatch):
    reg = {"entries": [{"file": "deadbeef.pdf", "pages": [
        {"page": 1, "expected_action": "ocr|both", "expected_entities": [],
         "consequence": "漏打", "correct_source": "ocr", "note": "水印层"}]}]}
    (tmp_path / "registry.json").write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("ROUTING_REGISTRY_DIR", str(tmp_path))
    entries = registry.load_registry()
    assert len(entries) == 1
    assert registry.expected_action_allows(entries[0].pages[0], "ocr") is True
    assert registry.expected_action_allows(entries[0].pages[0], "text_layer") is False


def _testdata_available() -> bool:
    return bool(registry.load_registry())


def test_registry_dir_present_must_load_entries():
    """门⑥ Important 挂账：目录在场 ⇒ 登记必须可解析，坏 JSON 不得静默蒸发成 skip。

    区分度门打在**目录存在性**上而不是 load 结果上：「目录不存在（``None``）⇒
    skip」是合法回落（CI 无 testdata）；「``_registry_dir()`` 解析到目录而
    ``load_registry()==[]``」= registry.json 缺失/坏 JSON/entries 空——种子判据
    ``test_seed_volumes_judge_actions_match_registry`` 会全部静默 skip，必须红。
    """
    found = registry._registry_dir()
    if found is None:
        pytest.skip("registry 目录不存在（CI 无 testdata）——skip 合法")
    entries = registry.load_registry()
    assert entries, (
        f"registry 目录在场（{found}）但 load_registry()==[]：registry.json 缺失/坏 JSON/"
        "entries 空 ⇒ 种子登记被静默清空（必须红，不得当作「无登记」skip）"
    )


def test_bad_json_distinguishable_from_missing_dir(tmp_path, monkeypatch):
    """坏 JSON 复现：目录在场可被单独观测（``_registry_dir()`` 非 None），
    loader 契约保持「坏 JSON 折算 []、不抛」——上一条真环境门据此才立得住。"""
    (tmp_path / "registry.json").write_text("{broken json", encoding="utf-8")
    monkeypatch.setenv("ROUTING_REGISTRY_DIR", str(tmp_path))
    assert registry._registry_dir() is not None, "目录存在且含 registry.json ⇒ 必须解析到目录"
    assert registry.load_registry() == [], "loader 契约：坏 JSON 折算 []（安全方向），由目录存在性门区分"


@pytest.mark.skipif(not _testdata_available(), reason="私有 testdata 不在 CI")
def test_seed_volumes_judge_actions_match_registry():
    """B2a 核心：种子卷真实 PDF → judge 动作落期望允许集。"""
    for entry in registry.load_registry():
        if entry.file_path is None:
            continue
        t = get_routing_thresholds()
        for page_spec in entry.pages:
            s = collect_l1(entry.file_path, int(page_spec["page"]))
            d = judge_page(s, t, routing_version=1)
            assert registry.expected_action_allows(page_spec, d.action), (
                entry.file_name, page_spec["page"], d.action, d.reason)
