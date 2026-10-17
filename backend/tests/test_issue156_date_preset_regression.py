"""Issue#156 回归闸：preset ``DATE`` 的单日期形态必须仍产出实体（走生产文本链）。

背景：门①.5 r1 冷审实测——把 ``match_items_indexed`` 的判据从「有参与命名组」改成
「有声明命名组」后，preset ``DATE``（单日期分支无命名组、区间分支有 ``seg_a/seg_b``）
的单日期实体被整条吞掉（欠遮盖）。修法=该规则只作用于真 ``re.Match`` 路径，子进程/
代理路径保留既有「回退整 match」契约。本用例钉住生产入口行为，防止再次误改。
"""
import json
import pathlib
import re

from app.services.hybrid_ner_service import HybridNERService

_PRESET = pathlib.Path(__file__).resolve().parents[1] / "config" / "preset_entity_types.json"


class _Type:
    def __init__(self, id: str, regex_pattern: str) -> None:
        self.id = id
        self.regex_pattern = regex_pattern
        self.enabled = True
        self.use_llm = True
        self.name = id


def test_preset_date_single_form_still_emits_entity() -> None:
    pattern = json.loads(_PRESET.read_text("utf-8"))["DATE"]["regex_pattern"]
    assert sorted(re.compile(pattern).groupindex) == ["seg_a", "seg_b"], (
        "preset DATE 的命名组形态变了，本回归闸的前提需重新评估"
    )
    service = HybridNERService(has_service_instance=None)
    date_type = _Type("DATE", pattern)
    for text, expected in [
        ("本院于2022年1月25日立案受理", "2022年1月25日"),
        ("本院于二〇二二年一月二十五日立案受理", "二〇二二年一月二十五日"),
    ]:
        texts = [e.text for e in service._custom_regex_extract(text, [date_type])]
        assert any(expected in t for t in texts), (text, texts)
    ranged = [e.text for e in service._custom_regex_extract("本案于2022年1月1日至1月31日期间审理", [date_type])]
    assert len(ranged) >= 2, ranged
