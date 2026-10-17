"""Issue#156 项③-A11：过遮盖方向——值变窄后不得框到别处更多位置。

PR#152 评审 A11 挂账项。判据：`find_value_spans` 返回的每个区间，其原文
归一后必须**恰好等于**被找的值（不得多吃字符）；值变窄后命中集必须收窄
（不得扩张到别处）。这条方向与「欠遮盖」相反，是误伤真实文本的那一侧。
"""
from app.services import text_layer_docline_service as docline


def _stream_and_map(text: str):
    stream = text
    normalized, index_map = docline._normalized_stream(stream)
    return stream, normalized, index_map


def test_every_span_covers_exactly_the_value_not_more() -> None:
    stream, normalized, index_map = _stream_and_map("文号 １－１７ 号，另一处 1-17 号")
    spans = docline.find_value_spans(normalized, index_map, "1-17")
    assert spans, "归一后必须命中"
    for start, end in spans:
        assert docline._fold(stream[start:end]) == docline._fold("1-17"), (
            f"区间 {stream[start:end]!r} 多吃/少吃字符"
        )


def test_narrowing_a_value_narrows_the_hits_never_expands() -> None:
    stream, normalized, index_map = _stream_and_map("文号 １－１７ 号")
    wide = docline.find_value_spans(normalized, index_map, "1-17")
    narrow = docline.find_value_spans(normalized, index_map, "17")
    assert wide and narrow
    for start, end in wide + narrow:
        assert docline._fold(stream[start:end]) in {"1-17", "17"}


def test_narrowed_value_inside_a_longer_word_is_rejected_by_boundary_check() -> None:
    """「值变窄」最危险的形态：窄值落在长词内部 ⇒ 边界校验必须挡住。"""
    word = docline.TextLayerWord(50.0, 700.0, 90.0, 720.0, "张三丰")
    pages = [docline.TextLayerPage(no=1, width=595.0, height=842.0, words=[word])]
    stream, word_spans = docline.build_stream(pages)
    normalized, index_map = docline._normalized_stream(stream)
    starts = {span.start for span in word_spans}
    ends = {span.end for span in word_spans}
    bounded = docline.find_value_spans(
        normalized, index_map, "张三", word_starts=starts, word_ends=ends, stream=stream
    )
    assert bounded == [], "窄值落在长词内部必须零命中（否则过遮盖）"
