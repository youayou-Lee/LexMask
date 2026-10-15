"""内嵌文本层分类器的**可证伪矩阵**（Issue#115 C1 / 验收 D5）。

设计原则（对应门①.5 评审的反复批评）：每一条都必须是「改动没做时必然失败」的
判别断言，而不是恒真断言。因此本矩阵：

1. **每类坏层都对应一条真实成因**，并断言分类器把它降级；
2. **逐项钉住降级理由串**（`reasons`），而不是只断言 `severity != USABLE` ——
   否则删掉任一条阈值检查、只要还有另一条兜着，测试仍会绿（评审在 v2 实测过这个漏洞：
   关掉 garbage / short / avg_word_len 三条后 16 个测试仍全部通过）；
3. 末尾有一条**正常层必须判 USABLE** 的防过度收紧断言。

坏层成因（真实案卷里都出现过）：
    空层       —— 扫描件根本没有文本层
    稀疏层     —— 只认出零星几个字（噪声被当成文本层）
    乱码层     —— 替换字符 / 私用区字符（编码坏了）
    碎片层     —— 每个汉字都被当成一个独立「词」（层是按字切的）
    粘连层     —— 整页被拼成一个「词」（层是按页切的，没有断词）
    错位层     —— 词框与页面墨迹不重合（坐标变换错了，如未乘 rotation matrix）
"""
from __future__ import annotations

import pytest

from app.services.text_layer_quality import (
    SUSPECT,
    UNUSABLE,
    USABLE,
    classify,
    compute_metrics,
)

# 一页「正常」文本层：20 个词、平均 8 字 ⇒ 160 字/页，落在全部阈值内。
_GOOD_WORDS = [
    "犯罪嫌疑人的", "姓名是张三丰", "身份证号", "110101199001011234",
    "户籍地址为", "北京市朝阳区", "某某路某号", "联系电话",
    "13800000000", "工作单位", "某某科技有限公司", "银行卡号",
    "6222020000000000000", "车牌号为", "京A12345", "案号为",
    "（2022）京0101", "刑初123号", "办案单位为", "北京市公安局朝阳分局",
]


def _good_pages(n: int = 3) -> list[list[str]]:
    return [list(_GOOD_WORDS) for _ in range(n)]


def test_healthy_layer_is_usable() -> None:
    """防过度收紧：正常层必须判 USABLE（否则本特性等于从不生效）。"""
    v = classify(compute_metrics(_good_pages(), ink_alignment=0.18))
    assert v.severity == USABLE, v.as_dict()
    assert v.usable is True


def test_empty_layer_is_unusable() -> None:
    v = classify(compute_metrics([[], [], []]))
    assert v.severity == UNUSABLE
    assert v.reasons == ("empty_text_layer",)


def test_sparse_layer_is_unusable() -> None:
    """噪声被当成文本层：3 页共 9 个字。"""
    v = classify(compute_metrics([["无"], ["字"], ["体"]]))
    assert v.severity == UNUSABLE
    assert v.reasons[0].startswith("sparse_text_layer:")


def test_garbled_layer_is_suspect() -> None:
    """编码坏掉：大量 U+FFFD 替换字符。"""
    bad = [[("\ufffd" * 8) for _ in range(20)] for _ in range(3)]
    v = classify(compute_metrics(bad, ink_alignment=0.18))
    assert v.severity == SUSPECT
    assert any(r.startswith("garbage_chars:") for r in v.reasons), v.reasons


def test_char_fragmented_layer_is_suspect() -> None:
    """层按字切：每个汉字都是独立「词」⇒ 平均词长 1。"""
    bad = [[c for c in "".join(_GOOD_WORDS)] for _ in range(3)]
    m = compute_metrics(bad, ink_alignment=0.18)
    v = classify(m)
    assert m.avg_word_len < 1.6
    assert v.severity == SUSPECT
    assert any(r.startswith("avg_word_len:") for r in v.reasons), v.reasons
    assert any(r.startswith("short_words:") for r in v.reasons), v.reasons


def test_glued_layer_is_suspect() -> None:
    """层按页切：整页拼成一个「词」⇒ 单「词」长 2000+。

    真实卷最长「词」实测 48 字（60 页样本 max=48、p99=34），所以 2000+ 一眼可辨。
    这条是**新增判据**（`TEXT_LAYER_MAX_WORD_CHARS`）的判别锚点：删掉该判据即红。
    """
    glued = [["".join(_GOOD_WORDS) * 20] for _ in range(3)]
    m = compute_metrics(glued, ink_alignment=0.18)
    v = classify(m)
    assert m.max_word_len > 400
    assert v.severity == SUSPECT
    assert any(r.startswith("glued_words:") for r in v.reasons), v.reasons


def test_misaligned_layer_is_suspect() -> None:
    """词框与墨迹不重合（坐标变换缺失，如未乘 page.rotation_matrix）。"""
    v = classify(compute_metrics(_good_pages(), ink_alignment=0.0))
    assert v.severity == SUSPECT
    assert any(r.startswith("ink_alignment:") for r in v.reasons), v.reasons


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("garbage", "garbage_chars:"),
        ("fragmented", "avg_word_len:"),
        ("glued", "glued_words:"),
        ("misaligned", "ink_alignment:"),
    ],
)
def test_each_threshold_has_a_dedicated_red_anchor(case: str, expected_reason: str) -> None:
    """阈值与判别锚点一一对应：任一条被删除/放水，本参数化用例必红。

    这条防止「修一个 bug 顺带废掉另一个判据」——评审 v2 实测过的漏洞类别。
    """
    pages = {
        "garbage": [[("\ufffd" * 8) for _ in range(20)] for _ in range(3)],
        "fragmented": [[c for c in "".join(_GOOD_WORDS)] for _ in range(3)],
        "glued": [["".join(_GOOD_WORDS) * 20] for _ in range(3)],
        "misaligned": _good_pages(),
    }[case]
    ink = 0.0 if case == "misaligned" else 0.18
    v = classify(compute_metrics(pages, ink_alignment=ink))
    assert v.severity == SUSPECT, (case, v.as_dict())
    assert any(r.startswith(expected_reason) for r in v.reasons), (case, v.reasons)