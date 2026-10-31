"""Issue#212 AC3：双表示守卫（v1 `*_content_list.json` 作第二来源）。

管线必须：两路正文量相差 >5% 时**指名**胜者并告警；v2 丢表时回落 v1；v1 缺失
（或水印放大造成 v1 偏大）时留在 v2 并告警；两路皆空 ⇒ FAILED（parse_no_text），
不得 "0 映射静默就绪"。全部 fake 依赖，无网络。
"""

import fitz
import pytest

from app.services.agent_md_pipeline_service import AgentMdPipelineService
from app.services.agent_md_types import TaskState
from app.services.mineru_parse_client import MineruResult

_HTML = ("<table><tr><td>户名</td><td>张某某</td></tr>"
         "<tr><td>账号</td><td>6222020200012345678</td></tr></table>")
_HTML_TEXT = "户名 | 张某某\n账号 | 6222020200012345678"


def _v2_page(*, html: str | None = _HTML, extra_text: str = "") -> list[list[dict]]:
    blocks: list[dict] = []
    if html is not None:
        blocks.append({"type": "table",
                       "content": {"html": html, "image_source": {"path": "images/t.jpg"}},
                       "bbox": [0, 0, 100, 100]})
    else:
        blocks.append({"type": "table",
                       "content": {"image_source": {"path": "images/t.jpg"},
                                   "table_caption": [], "table_footnote": []},
                       "bbox": [0, 0, 100, 100]})
    if extra_text:
        blocks.append({"type": "paragraph",
                       "content": {"paragraph_content": [{"type": "text", "content": extra_text}]},
                       "bbox": [0, 100, 100, 120]})
    return [blocks]


def _v1_flat(*, table_body: str | None = _HTML, extra_text: str = "") -> list[dict]:
    items: list[dict] = []
    if table_body is not None:
        items.append({"type": "table", "table_body": table_body, "page_idx": 0,
                      "bbox": [0, 0, 100, 100]})
    if extra_text:
        items.append({"type": "text", "text": extra_text, "page_idx": 0})
    return items


class _FakeMineru:
    def __init__(self, v2, v1):
        self._v2 = v2
        self._v1 = v1

    async def submit(self, file_bytes, filename):
        return "job-1"

    async def wait_result(self, task_id, on_progress=None):
        if on_progress:
            on_progress(1, 1)
        return MineruResult(md="# 合成", content_list_v2=self._v2,
                            content_list_v1=self._v1, images={})


class _FakeOcr:
    def extract_text_boxes(self, image_bytes):
        return []


class _FakeNer:
    async def extract(self, text, entity_types):
        from app.models.entity_schemas import Entity

        ents = []
        for name in ("张某某",):
            idx = text.find(name)
            if idx >= 0:
                ents.append(Entity(id=name, text=name, type="PERSON", start=idx, end=idx + 3))
        return ents


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "a.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(p))
    doc.close()
    return str(p)


def _run(sample_pdf, tmp_path, *, v2, v1):
    svc = AgentMdPipelineService(
        mineru_client=_FakeMineru(v2, v1), ocr=_FakeOcr(), ner=_FakeNer(),
        entity_types=[], output_dir=str(tmp_path / "outputs"),
    )
    return svc.create_task_nowait(sample_pdf, "a.pdf", owner_id="u1")


def _body(task) -> str:
    return "\n".join(s.text for s in task.segs)


# ---- a) v2 丢表、v1 完整 ⇒ 回落 v1 ----


def test_v2_dropped_table_falls_back_to_v1(sample_pdf, tmp_path):
    task = _run(sample_pdf, tmp_path, v2=_v2_page(html=None), v1=_v1_flat())

    assert "6222020200012345678" in _body(task)
    assert any("dual_representation_shortfall" in w and "winner=content_list_v1" in w
               for w in task.warnings)
    assert task.dropped_blocks == {"empty_table": 1, "empty_table_pages": [0],
                                   "unhandled": {}, "source": "content_list_v1"}
    assert task.state == TaskState.MAPPING_READY
    assert task.entities  # fake NER 从 v1 正文里认出张某某
    # 门⑥ I3：默认九类必须真的写进 task（零实体闸的豁免条件依赖它非空）
    assert task.entity_types_used, "entity_types_used 不能为空——否则零实体闸对所有任务豁免"


# ---- b) 水印/重复放大：v1 侧字符暴涨 ⇒ 不得盲取较大者，留在 v2 ----


def test_watermark_bloated_v1_does_not_win(sample_pdf, tmp_path):
    task = _run(sample_pdf, tmp_path, v2=_v2_page(), v1=_v1_flat(extra_text="重复水印文字" * 500))

    assert _body(task).count("6222020200012345678") == 1
    assert "重复水印文字" not in _body(task)
    assert task.chars < 200  # 正文量来自 v2（未修的 v1 直取会把 3k 水印计入）
    # 字面冻结：v1 = 表格 33 字 + 6×500 水印 = 3033，v2 = 33；不采信 v1
    assert "dual_representation_shortfall: winner=content_list_v2 v1=3033 v2=33" in task.warnings
    assert task.state == TaskState.MAPPING_READY


# ---- c) v1 缺失 ⇒ 用 v2 但必须报"没有第二来源" ----


def test_missing_v1_reports_unavailable(sample_pdf, tmp_path):
    task = _run(sample_pdf, tmp_path, v2=_v2_page(), v1=[])

    assert "6222020200012345678" in _body(task)
    assert any("dual_representation_unavailable" in w for w in task.warnings)
    assert task.dropped_blocks["source"] == "content_list_v2"
    assert task.state == TaskState.MAPPING_READY


# ---- d) 两路皆空 ⇒ 显式失败，禁静默就绪 ----


def test_both_representations_empty_fails_loudly(sample_pdf, tmp_path):
    # 两路都存在但表格正文为空（= 解析成功、正文全空）：不是"v1 缺失"，必须显式失败
    v1 = [{"type": "table", "table_body": "", "page_idx": 0, "bbox": [0, 0, 100, 100]}]
    task = _run(sample_pdf, tmp_path, v2=_v2_page(html=None), v1=v1)

    assert task.state == TaskState.FAILED
    assert task.message.startswith("parse_no_text")
    assert task.mapping == []
    assert task.dropped_blocks == {"empty_table": 1, "empty_table_pages": [0],
                                   "unhandled": {}, "source": "none"}


# ---- e) v2 缺失、v1 完整 ⇒ 用 v1（管线主路径整版缺失也不得归零）----


def test_missing_v2_uses_v1(sample_pdf, tmp_path):
    task = _run(sample_pdf, tmp_path, v2=[], v1=_v1_flat())

    assert "6222020200012345678" in _body(task)
    assert any("winner=content_list_v1" in w and "v2=0" in w for w in task.warnings)
    assert task.dropped_blocks["source"] == "content_list_v1"
    assert task.state == TaskState.MAPPING_READY


# ---- f) 两路接近 ⇒ 用 v2 且不产生任何守卫告警（防"永远告警"）----


def test_close_representations_use_v2_without_warn(sample_pdf, tmp_path):
    task = _run(sample_pdf, tmp_path, v2=_v2_page(), v1=_v1_flat())

    assert "6222020200012345678" in _body(task)
    assert not any("dual_representation_" in w for w in task.warnings)
    assert task.dropped_blocks == {"empty_table": 0, "empty_table_pages": [],
                                   "unhandled": {}, "source": "content_list_v2"}

# ---- g) v1 存在但正文全空（≠ v1 缺失）⇒ 用 v2，绝不把 v1 空串当正文 ----


def test_v1_present_but_empty_text_uses_v2(sample_pdf, tmp_path):
    v1 = [{"type": "table", "table_body": "", "page_idx": 0, "bbox": [0, 0, 100, 100]}]
    task = _run(sample_pdf, tmp_path, v2=_v2_page(), v1=v1)

    assert "6222020200012345678" in _body(task)
    assert task.chars == 33
    # 字面冻结：v1=0（非缺失，不得报 unavailable）
    assert "dual_representation_shortfall: winner=content_list_v2 v1=0 v2=33" in task.warnings
    assert not any("dual_representation_unavailable" in w for w in task.warnings)
    assert task.dropped_blocks["source"] == "content_list_v2"


# ---- j) I2：v2 部分丢表（有正文但少一截 + 有空表）⇒ 回落 v1 ----


def test_partially_dropped_v2_falls_back_to_v1(sample_pdf, tmp_path):
    """v2 丢了部分表格（还有一张带 html）时，走"short + 有空表"分支回落 v1。

    这是"部分丢表"事故的唯一保命路径（整版丢表由 v2_chars==0 分支处理），门⑥
    发现该分支零判据（M9 SURVIVED），故补此例。
    """
    v2 = _v2_page(html=_HTML)
    v2[0].append({"type": "table",
                  "content": {"image_source": {"path": "images/x.jpg"}},
                  "bbox": [0, 120, 100, 220]})
    v1 = _v1_flat(extra_text="余额 134.37 元")

    task = _run(sample_pdf, tmp_path, v2=v2, v1=v1)

    assert task.dropped_blocks["source"] == "content_list_v1"
    assert task.dropped_blocks["empty_table"] == 1  # 仍报 v2 事故侧计数
    assert "134.37" in _body(task)  # v1 独有内容确实进来了
    assert any("winner=content_list_v1" in w and "v2=33" in w for w in task.warnings)
    assert task.state == TaskState.MAPPING_READY


# ---- k) I6：v1 缺失且 v2 正文全空 ⇒ 同样是 parse_no_text，不是"0 实体就绪" ----


def test_missing_v1_and_empty_v2_fails_loudly(sample_pdf, tmp_path):
    task = _run(sample_pdf, tmp_path, v2=_v2_page(html=None), v1=[])

    assert task.state == TaskState.FAILED
    assert task.message.startswith("parse_no_text")
    assert task.dropped_blocks["source"] == "none"


# ---- l) m5：GUARD_RATIO=0.95 的"恰等"与"略小"边界 ----


def _text_page(text: str) -> list[list[dict]]:
    return [[{"type": "paragraph",
              "content": {"paragraph_content": [{"type": "text", "content": text}]},
              "bbox": [0, 0, 100, 100]}]]


def _text_flat(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "page_idx": 0}]


def test_guard_ratio_exact_boundary_does_not_short(sample_pdf, tmp_path):
    """v2 == 0.95×v1 恰等：不算 short，不回落、不告警。"""
    task = _run(sample_pdf, tmp_path, v2=_text_page("B" * 190), v1=_text_flat("B" * 200))

    assert task.dropped_blocks["source"] == "content_list_v2"
    assert not any("dual_representation_" in w for w in task.warnings)


def test_guard_ratio_just_below_boundary_is_short(sample_pdf, tmp_path):
    """v2 = 0.945×v1（略小）：short 成立，但因 v2 没丢表 ⇒ 留在 v2 并指名数值。"""
    task = _run(sample_pdf, tmp_path, v2=_text_page("B" * 189), v1=_text_flat("B" * 200))

    assert task.dropped_blocks["source"] == "content_list_v2"
    assert "dual_representation_shortfall: winner=content_list_v2 v1=200 v2=189" in task.warnings


# ---- h) v1 明显少于 v2（v2 未丢表、非 short 方向）⇒ 仍用 v2 并指明数值 ----


def test_v1_shorter_than_v2_keeps_v2_with_named_values(sample_pdf, tmp_path):
    task = _run(sample_pdf, tmp_path, v2=_v2_page(), v1=_v1_flat(table_body=None, extra_text="正文"))

    assert "6222020200012345678" in _body(task)
    assert "dual_representation_shortfall: winner=content_list_v2 v1=2 v2=33" in task.warnings
    assert task.dropped_blocks["source"] == "content_list_v2"


# ---- i) 并发两任务互不串味（dropped_blocks/warnings 是 per-run 状态）----


def test_concurrent_tasks_do_not_cross_contaminate(sample_pdf, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def run_case(v2, v1, tag):
        d = tmp_path / tag
        d.mkdir()
        return _run(sample_pdf, d, v2=v2, v1=v1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_drop = pool.submit(run_case, _v2_page(html=None), _v1_flat(), "a")
        f_ok = pool.submit(run_case, _v2_page(), _v1_flat(), "b")
        t_drop, t_ok = f_drop.result(), f_ok.result()

    assert t_drop.dropped_blocks == {"empty_table": 1, "empty_table_pages": [0],
                                     "unhandled": {}, "source": "content_list_v1"}
    assert any("winner=content_list_v1" in w for w in t_drop.warnings)

    assert t_ok.dropped_blocks == {"empty_table": 0, "empty_table_pages": [],
                                   "unhandled": {}, "source": "content_list_v2"}
    assert not any("dual_representation_" in w for w in t_ok.warnings)


# ---- Issue#219：4.x docvortex.middle 表示分支与失败形态矩阵（AC3/AC5）----

def _v4_doc(pages_blocks, *, schema_version="2.0"):
    return {
        "schema": "docvortex.middle", "schema_version": schema_version,
        "metadata": {"producer": {"name": "mineru", "version": "4.1.3"}},
        "pages": [
            {"page_idx": idx, "blocks": blocks} for idx, blocks in pages_blocks
        ],
        "is_full_document": True,
    }


def _v4_text_block(text):
    return {"type": "text", "index": 0, "bbox": [0, 0, 1, 1],
            "content": [{"type": "text", "content": text}]}


class _FakeMineruV4:
    def __init__(self, docs, md="", v2=None, v1=None, unknown=None):
        self._docs = docs
        self._md = md
        self._v2 = v2 or []
        self._v1 = v1 or []
        self._unknown = unknown or []

    async def submit(self, file_bytes, filename):
        return "job-1"

    async def wait_result(self, task_id, on_progress=None):
        if on_progress:
            on_progress(1, 1)
        return MineruResult(md=self._md, content_list_v2=self._v2,
                            content_list_v1=self._v1, images={},
                            mineru4_docs=self._docs, unknown_jsons=self._unknown)


def _run_v4(sample_pdf, tmp_path, fake):
    svc = AgentMdPipelineService(
        mineru_client=fake, ocr=_FakeOcr(), ner=_FakeNer(),
        entity_types=[], output_dir=str(tmp_path / "outputs"),
    )
    return svc.create_task_nowait(sample_pdf, "a.pdf", owner_id="u1")


def test_v4_happy_path_maps_and_names_source(sample_pdf, tmp_path):
    doc = _v4_doc([(0, [_v4_text_block("原告：张示例，住示例街道1号。")])])
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4([doc], md="原告：张示例，住示例街道1号。"))

    assert task.state == TaskState.MAPPING_READY
    assert "张示例" in _body(task)
    assert task.dropped_blocks["source"] == "docvortex_middle"
    assert not any("dual_representation_" in w for w in task.warnings)  # 单表示，守卫空转
    assert not any("docvortex_shortfall" in w for w in task.warnings)  # 比值 1.0 ≥ 0.7


def test_v4_multiple_distinct_docs_fail_closed(sample_pdf, tmp_path):
    d1 = _v4_doc([(0, [_v4_text_block("甲卷正文")])], schema_version="2.0")
    d2 = _v4_doc([(0, [_v4_text_block("乙卷正文")])], schema_version="2.0")
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4([d1, d2], md="x"))

    assert task.state == TaskState.FAILED
    assert task.message.startswith("parse_no_text")
    assert any("mineru4_multiple_docs" in w for w in task.warnings)
    assert task.dropped_blocks["source"] == "docvortex_middle"


def test_v4_duplicate_pages_dedupe_succeeds(sample_pdf, tmp_path):
    """C1/R1 正例：doclib 双文件 pages 相同 ⇒ 去重成功出稿（metadata 差异不判死）。"""
    doc_a = _v4_doc([(0, [_v4_text_block("唯一正文：原告张示例")])])
    import copy
    doc_b = copy.deepcopy(doc_a)
    doc_b["metadata"]["generated_at"] = "差异时间戳"  # 仅 metadata 不同
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4([doc_a, doc_b], md="唯一正文：原告张示例"))

    assert task.state == TaskState.MAPPING_READY
    assert _body(task).count("张示例") == 1  # 不翻倍
    assert not any("mineru4_multiple_docs" in w for w in task.warnings)


def test_v4_zero_text_fails_loudly(sample_pdf, tmp_path):
    doc = _v4_doc([(0, [{"type": "image", "index": 0, "content": [
        {"type": "image_body", "content": ""}]}])])
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4([doc], md=""))

    assert task.state == TaskState.FAILED
    assert task.message.startswith("parse_no_text")
    assert task.dropped_blocks["source"] == "docvortex_middle"


def test_v4_shortfall_vs_md_warns(sample_pdf, tmp_path):
    """I2 比值守卫：segs 正文显著少于 zip 内官方 md ⇒ docvortex_shortfall 点名数值。"""
    body = "正文只有一句"
    doc = _v4_doc([(0, [_v4_text_block(body)])])
    md = "本应更多的正文。" * 100  # md 参照 700 字，v4 只有 6 字
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4([doc], md=md))

    assert task.state == TaskState.MAPPING_READY  # 有正文不判死，warning-only
    assert any(w.startswith("docvortex_shortfall:") and "v4=" in w and "md=" in w
               for w in task.warnings)


def test_v4_md_missing_guard_skips_loudly(sample_pdf, tmp_path):
    doc = _v4_doc([(0, [_v4_text_block("原告：张示例")])])
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4([doc], md=""))

    assert task.state == TaskState.MAPPING_READY
    assert any("docvortex_md_missing" in w for w in task.warnings)  # R4：显式跳过不静默
    assert not any("docvortex_shortfall" in w for w in task.warnings)


def test_v4_with_unknown_json_warns_named(sample_pdf, tmp_path):
    doc = _v4_doc([(0, [_v4_text_block("原告：张示例")])])
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4([doc], md="原告：张示例",
                                                       unknown=["case01_weird.json"]))

    assert task.state == TaskState.MAPPING_READY
    assert any("case01_weird.json" in w for w in task.warnings)


def test_mixed_v4_and_3x_v4_wins_with_warning(sample_pdf, tmp_path):
    """M3①：混合产物 v4 优先，但不得静默丢弃 3.x 表示。"""
    doc = _v4_doc([(0, [_v4_text_block("v4 正文：被告李演示")])])
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4(
        [doc], md="x", v2=_v2_page(), v1=_v1_flat()))

    assert task.state == TaskState.MAPPING_READY
    assert "李演示" in _body(task) and "6222020200012345678" not in _body(task)
    assert task.dropped_blocks["source"] == "docvortex_middle"
    assert any("mixed_schema_v4_wins" in w for w in task.warnings)


def test_unknown_json_with_empty_3x_fails_source_unknown(sample_pdf, tmp_path):
    """AC5③：v4 缺失 + v1/v2 空 + unknown 非空 ⇒ parse_no_text 且点名文件。"""
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4(
        [], md="x", v2=[], v1=[], unknown=["mystery.json"]))

    assert task.state == TaskState.FAILED
    assert task.message.startswith("parse_no_text")
    assert "mystery.json" in task.message
    assert task.dropped_blocks["source"] == "unknown"


def test_unknown_json_with_valid_3x_warns_but_completes(sample_pdf, tmp_path):
    """R3 对称行为：3.x 有正文出稿，但 unknown 必须点名不静默。"""
    task = _run_v4(sample_pdf, tmp_path, _FakeMineruV4(
        [], md="x", v2=_v2_page(), v1=_v1_flat(), unknown=["mystery.json"]))

    assert task.state == TaskState.MAPPING_READY
    assert "6222020200012345678" in _body(task)
    assert any("mystery.json" in w for w in task.warnings)
