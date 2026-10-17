"""Issue#96：公章来源切 MinerU —— 取数、换源与回落。

覆盖三层：
1. `extract_seal_boxes`：content_list_v2 → 页级 0-1 章框（含 `page_idx` 为 null 的
   本环境实测形态、两种块形、畸形块与空/漂移响应）；
2. `MineruSealSource`：按文件一次 sidecar 任务 + 进程级缓存 + 失败态不重试；
3. `VisionService._apply_mineru_seal_source`：换源语义（LA 章框整体替换、签名与
   VL 通道不动、`confidence=None`、失败态按开关回落）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.schemas import BoundingBox, FileType
from app.services.vision import mineru_seal_source as mss
from app.services.vision.mineru_seal_source import (
    STATUS_FAILED,
    STATUS_MISSING_V2,
    STATUS_OK,
    STATUS_SHAPE_DRIFT,
    MineruSealSource,
    extract_seal_boxes,
)
from app.services.vision_service import VisionService


def _seal_block(x0: int = 699, y0: int = 9, x1: int = 895, y1: int = 159) -> dict:
    """MinerU 实测形态：bbox 是 0-1000 的角点 [x0,y0,x1,y1]，page_idx 为 null。"""
    return {"type": "image", "sub_type": "seal", "bbox": [x0, y0, x1, y1], "page_idx": None}


def _service() -> VisionService:
    """不走 __init__（避免构造真实下游服务），只借方法。"""
    svc = VisionService.__new__(VisionService)
    svc.last_seal_source_status = ""  # type: ignore[attr-defined]
    svc.last_seal_source_effective = ""  # type: ignore[attr-defined]
    svc._seal_source_logged = set()  # type: ignore[attr-defined]
    return svc


class _FakeSource:
    def __init__(self, boxes_by_page: dict[int, list[tuple[float, float, float, float]]], status: str):
        self.boxes_by_page = boxes_by_page
        self.status = status
        self.calls: list[tuple[str, int]] = []

    async def page_seals(self, cache_key, load_bytes, page, *, filename=None):
        self.calls.append((cache_key, page))
        return list(self.boxes_by_page.get(int(page), [])), self.status


class _FakeClient:
    def __init__(self, content_list_v2, *, fail: bool = False):
        self.submit_calls = 0
        self._content_list_v2 = content_list_v2
        self._fail = fail

    async def submit(self, payload: bytes, filename: str) -> str:
        self.submit_calls += 1
        if self._fail:
            raise RuntimeError("sidecar down")
        return "task-1"

    async def wait_result(self, task_id: str):
        if self._fail:
            raise RuntimeError("sidecar down")
        return SimpleNamespace(content_list_v2=self._content_list_v2)


# --------------------------------------------------------------------------
# 1. extract_seal_boxes
# --------------------------------------------------------------------------


def test_extract_maps_pages_by_outer_index_and_normalizes_bbox():
    content = [[_seal_block()], [{"type": "paragraph"}], [_seal_block(0, 0, 1000, 1000)]]
    boxes_by_page, status, warnings = _extract3(content)

    assert status == STATUS_OK
    assert warnings == []
    assert sorted(boxes_by_page) == [0, 2]
    assert boxes_by_page[0][0] == pytest.approx((0.699, 0.009, 0.196, 0.15))
    assert boxes_by_page[2][0] == pytest.approx((0.0, 0.0, 1.0, 1.0))


def test_extract_accepts_type_seal_variant():
    content = [[{"type": "seal", "bbox": [500, 500, 600, 600]}]]
    boxes_by_page, status, _ = _extract3(content)

    assert status == STATUS_OK
    assert boxes_by_page[0][0] == pytest.approx((0.5, 0.5, 0.1, 0.1))


def test_extract_skips_malformed_candidates_with_warning():
    content = [
        [
            {"type": "image", "sub_type": "seal", "bbox": [1, 2, 3]},
            {"type": "image", "sub_type": "seal", "bbox": ["a", "b", "c", "d"]},
            _seal_block(10, 20, 30, 40),
        ]
    ]
    boxes_by_page, status, warnings = _extract3(content)

    assert status == STATUS_OK
    assert boxes_by_page[0][0] == pytest.approx((0.01, 0.02, 0.02, 0.02))
    assert len(warnings) == 2


def test_extract_reports_missing_v2_and_shape_drift():
    assert _extract3(None) == ({}, STATUS_MISSING_V2, ["content_list_v2 is None"])
    assert _extract3([]) == ({}, STATUS_OK, [])
    boxes_by_page, status, warnings = _extract3(["not-a-list", _seal_block()])
    assert boxes_by_page == {}
    assert status == STATUS_SHAPE_DRIFT
    assert warnings

    boxes_by_page, status, warnings = _extract3(["not-a-list", [_seal_block()]])
    assert boxes_by_page[1][0] == pytest.approx((0.699, 0.009, 0.196, 0.15))
    assert status == STATUS_OK
    assert warnings  # 外层下标 0 不是页列表


def test_extract_rejects_degenerate_bbox():
    content = [[{"type": "image", "sub_type": "seal", "bbox": [100, 100, 100, 200]}]]
    boxes_by_page, status, warnings, drifted = extract_seal_boxes(content)

    assert status == STATUS_OK
    assert boxes_by_page == {}
    assert drifted == frozenset({0})
    assert len(warnings) == 2  # 畸形候选块 + 该页无一可用（态⑤）


def _extract3(content):
    """r6 C-4 后 ``extract_seal_boxes`` 返回 4 元组；旧断言的兼容投影。"""
    boxes, status, warnings, _drifted = extract_seal_boxes(content)
    return boxes, status, warnings


# --------------------------------------------------------------------------
# 2. MineruSealSource
# --------------------------------------------------------------------------


def test_page_seals_runs_one_sidecar_task_per_file():
    """生产页号 1 基 → 外层下标 0 基：第 1 页取 ``content_list_v2[0]``（r6 C-1）。"""
    client = _FakeClient([[_seal_block()], [{"type": "paragraph"}]])
    source = MineruSealSource(client_factory=lambda: client)

    async def scenario():
        first = await source.page_seals("k", lambda: _abytes(b"pdf"), 1, filename="a.pdf")
        second = await source.page_seals("k", lambda: _abytes(b"pdf"), 2, filename="a.pdf")
        return first, second

    (boxes_page1, status1), (boxes_page2, status2) = asyncio.run(scenario())

    assert client.submit_calls == 1
    assert status1 == status2 == STATUS_OK
    assert boxes_page1[0] == pytest.approx((0.699, 0.009, 0.196, 0.15))
    assert boxes_page2 == []
    assert source.stats["files"] == 1
    assert source.stats["ok"] == 1


def test_page_seals_maps_production_page_number_to_outer_index():
    """判别性探针（r6 P1）：两页各一章，1 基页号不得错位到下一页。

    变异「去掉 page - 1」必须让本用例转红；否则页号口径又会被静默写错。
    """
    client = _FakeClient([[_seal_block(699, 9, 895, 159)], [_seal_block(100, 100, 300, 300)]])
    source = MineruSealSource(client_factory=lambda: client)

    async def scenario():
        first = await source.page_seals("k", lambda: _abytes(b"pdf"), 1)
        second = await source.page_seals("k", lambda: _abytes(b"pdf"), 2)
        return first, second

    (boxes1, status1), (boxes2, status2) = asyncio.run(scenario())

    assert status1 == status2 == STATUS_OK
    assert boxes1 == [pytest.approx((0.699, 0.009, 0.196, 0.15))]
    assert boxes2 == [pytest.approx((0.1, 0.1, 0.2, 0.2))]


def test_page_seals_reports_shape_drift_for_a_drifted_page():
    """该页有章候选块却无一可用 ⇒ 该页按块形漂移处置（态⑤），其余页不受影响。"""
    bad = {"type": "image", "sub_type": "seal", "bbox": ["a", "b", "c", "d"], "page_idx": None}
    client = _FakeClient([[bad], [_seal_block()]])
    source = MineruSealSource(client_factory=lambda: client)

    async def scenario():
        return (
            await source.page_seals("k", lambda: _abytes(b"pdf"), 1),
            await source.page_seals("k", lambda: _abytes(b"pdf"), 2),
        )

    (boxes1, status1), (boxes2, status2) = asyncio.run(scenario())

    assert (boxes1, status1) == ([], STATUS_SHAPE_DRIFT)
    assert boxes2 and status2 == STATUS_OK
    assert source.stats["drifted_pages"] == 1


def test_page_seals_returns_failed_and_caches_failure():
    client = _FakeClient([], fail=True)
    source = MineruSealSource(client_factory=lambda: client)

    async def scenario():
        first = await source.page_seals("k", lambda: _abytes(b"pdf"), 1)
        second = await source.page_seals("k", lambda: _abytes(b"pdf"), 1)
        return first, second

    (boxes_a, status_a), (boxes_b, status_b) = asyncio.run(scenario())

    assert (boxes_a, status_a) == ([], STATUS_FAILED)
    assert (boxes_b, status_b) == ([], STATUS_FAILED)
    assert client.submit_calls == 1  # 失败态不重试
    assert source.stats["failed"] == 1


def test_page_seals_marks_failed_when_bytes_unreadable():
    source = MineruSealSource(client_factory=lambda: _FakeClient([]))

    async def boom() -> bytes:
        raise OSError("gone")

    boxes, status = asyncio.run(source.page_seals("k", boom, 1))

    assert (boxes, status) == ([], STATUS_FAILED)
    assert source.stats["failed"] == 1


async def _abytes(data: bytes) -> bytes:
    return data


# --------------------------------------------------------------------------
# 3. VisionService._apply_mineru_seal_source
# --------------------------------------------------------------------------

_VL_SEAL = BoundingBox(
    id="ocr_seal_1",
    x=0.70,
    y=0.01,
    width=0.19,
    height=0.15,
    type="official_seal",
    text="公章",
    page=1,
    confidence=0.9,
    source="visual_features",
    source_detail="paddleocr_vl:seal",
)
_LA_SEAL = BoundingBox(
    id="la_seal_1",
    x=0.71,
    y=0.02,
    width=0.18,
    height=0.14,
    type="official_seal",
    text="公章",
    page=1,
    confidence=0.77,
    source="visual_features",
    source_detail="locate_anything:detect",
)
_LA_SIGNATURE = BoundingBox(
    id="la_sig_1",
    x=0.1,
    y=0.8,
    width=0.2,
    height=0.1,
    type="signature",
    text="签名",
    page=1,
    confidence=0.5,
    source="visual_features",
    source_detail="locate_anything:signature",
)


def _apply(monkeypatch, tmp_path, source, *, source_name="mineru", fallback=True, file_type=FileType.PDF):
    monkeypatch.setattr(settings, "VISUAL_SEAL_SOURCE", source_name, raising=False)
    monkeypatch.setattr(settings, "VISUAL_SEAL_MINERU_FALLBACK_TO_LA", fallback, raising=False)
    monkeypatch.setattr(mss, "_source", source)

    path = tmp_path / "page.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    svc = _service()
    boxes, applied = asyncio.run(
        _run_apply(svc, [ _VL_SEAL, _LA_SEAL, _LA_SIGNATURE], str(path), file_type)
    )
    return svc, boxes, applied


async def _run_apply(svc, boxes, path, file_type):
    out = await svc._apply_mineru_seal_source(
        list(boxes), file_path=path, file_type=file_type, page=1
    )
    return out, svc.last_seal_source_status


def test_apply_replaces_la_seals_and_keeps_vl_and_signature(monkeypatch, tmp_path):
    source = _FakeSource({1: [(0.5, 0.5, 0.2, 0.2)]}, STATUS_OK)

    svc, boxes, status = _apply(monkeypatch, tmp_path, source)

    assert status == STATUS_OK
    assert source.calls and source.calls[0][1] == 1
    ids = [box.id for box in boxes]
    assert "la_seal_1" not in ids  # LA 章框被替换
    assert "la_sig_1" in ids  # 签名通道不动
    assert "ocr_seal_1" in ids  # VL 通道不动（未来 OCR_VL_ENABLED=1 时仍是权威）
    mineru_boxes = [box for box in boxes if box.source_detail == "mineru_seal"]
    assert len(mineru_boxes) == 1
    mineru = mineru_boxes[0]
    assert mineru.type == "official_seal"
    assert mineru.page == 1
    assert mineru.confidence is None  # 无 score ⇒ 不伪造常数
    assert (mineru.x, mineru.y, mineru.width, mineru.height) == (0.5, 0.5, 0.2, 0.2)
    assert svc.last_seal_source_status == STATUS_OK


def test_apply_drops_la_seals_when_mineru_reports_none(monkeypatch, tmp_path):
    source = _FakeSource({}, STATUS_OK)

    _svc, boxes, status = _apply(monkeypatch, tmp_path, source)

    assert status == STATUS_OK
    ids = [box.id for box in boxes]
    assert "la_seal_1" not in ids  # 权威「本页无章」⇒ 不回落
    assert "ocr_seal_1" in ids


def test_apply_keeps_la_seals_on_failure_when_fallback_enabled(monkeypatch, tmp_path):
    source = _FakeSource({}, STATUS_FAILED)

    _svc, boxes, status = _apply(monkeypatch, tmp_path, source, fallback=True)

    assert status == STATUS_FAILED
    ids = [box.id for box in boxes]
    assert "la_seal_1" in ids  # 保召回
    assert not [box for box in boxes if box.source_detail == "mineru_seal"]


def test_apply_drops_la_seals_on_failure_when_fallback_disabled(monkeypatch, tmp_path):
    source = _FakeSource({}, STATUS_SHAPE_DRIFT)

    _svc, boxes, status = _apply(monkeypatch, tmp_path, source, fallback=False)

    assert status == STATUS_SHAPE_DRIFT
    ids = [box.id for box in boxes]
    assert "la_seal_1" not in ids  # 严格换源
    assert "la_sig_1" in ids


def test_apply_is_noop_when_source_is_la(monkeypatch, tmp_path):
    source = _FakeSource({1: [(0.5, 0.5, 0.2, 0.2)]}, STATUS_OK)

    _svc, boxes, status = _apply(monkeypatch, tmp_path, source, source_name="la")

    assert status == ""  # 未触碰换源逻辑
    assert source.calls == []
    assert [box.id for box in boxes] == ["ocr_seal_1", "la_seal_1", "la_sig_1"]


def test_apply_skips_unsupported_file_types(monkeypatch, tmp_path):
    source = _FakeSource({1: [(0.5, 0.5, 0.2, 0.2)]}, STATUS_OK)

    _svc, boxes, status = _apply(monkeypatch, tmp_path, source, file_type=FileType.IMAGE)

    assert status == "unsupported"
    assert source.calls == []
    assert any(box.id == "la_seal_1" for box in boxes)


def test_apply_logs_effective_seal_source_for_mineru(monkeypatch, tmp_path, caplog):
    """AC5 日志级判据（r7 的「只写不读」）：生效来源 + 状态必须能从日志读出。"""
    import logging

    source = _FakeSource({1: [(0.5, 0.5, 0.2, 0.2)]}, STATUS_OK)
    with caplog.at_level(logging.INFO, logger="app.services.vision_service"):
        svc, _boxes, _status = _apply(monkeypatch, tmp_path, source)

    assert svc.last_seal_source_effective == "mineru"
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("visual_seal_source=mineru" in m and "status=ok" in m for m in messages)


def test_apply_logs_effective_seal_source_when_switched_off(monkeypatch, tmp_path, caplog):
    """回退到 la 时也必须能从日志看出「当前生效来源=la」（生产排查用）。"""
    import logging

    source = _FakeSource({}, STATUS_OK)
    with caplog.at_level(logging.INFO, logger="app.services.vision_service"):
        svc, _boxes, _status = _apply(monkeypatch, tmp_path, source, source_name="la")

    assert svc.last_seal_source_effective == "la"
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("visual_seal_source=la" in m for m in messages)


def test_extract_rejects_nan_components() -> None:
    """NaN 必须按 malformed 拦下（r10 I-1）：NaN 的比较恒为 False，会绕过范围判据。"""
    for raw in (
        [float("nan"), 0.01, float("nan"), 0.89],
        [0.1, float("nan"), 0.5, 0.6],
        [0.1, 0.2, float("nan"), 0.6],
        [0.1, 0.2, 0.5, float("nan")],
    ):
        boxes, status, warnings, drifted = extract_seal_boxes(
            [[{"type": "image", "sub_type": "seal", "bbox": raw}]]
        )
        assert boxes == {}, raw
        assert status == STATUS_OK, raw
        assert warnings and "without usable bbox" in warnings[0], (raw, warnings)
        assert drifted == {0}, raw


def test_default_la_path_never_touches_mineru_or_disk(monkeypatch, tmp_path) -> None:
    """默认关哨兵（r10 I-4）：source=la 时不得构造/调用 MinerU 通道、不得读源文件。

    门⑥ r10 自造变异 M-6a/M-6b（把 MinerU 构造与源文件读挪到早退之前）曾在
    仓内 24 例下全部 SURVIVED；本用例是它们的机器判据。
    """
    import builtins

    constructed: list[str] = []

    class _Forbidden:
        def __init__(self) -> None:
            constructed.append("constructed")

    monkeypatch.setattr(mss, "get_mineru_seal_source", _Forbidden)
    monkeypatch.setattr(settings, "VISUAL_SEAL_SOURCE", "la", raising=False)

    target = str(tmp_path / "page.pdf")
    opened: list[str] = []
    real_open = builtins.open

    def _spy_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _spy_open)

    svc = _service()
    boxes = [
        BoundingBox(
            id="la1",
            x=0.1,
            y=0.1,
            width=0.2,
            height=0.2,
            type="official_seal",
            page=1,
            source="visual_features",
            source_detail="locate_anything:detect",
        )
    ]
    out = asyncio.run(
        svc._apply_mineru_seal_source(boxes, file_path=target, file_type=FileType.PDF, page=1)
    )

    assert constructed == [], "默认 la 下不得构造 MinerU 通道"
    assert [p for p in opened if target in p] == [], "默认 la 下不得读源文件"
    assert out is boxes, "默认路径必须原样返回同一个 list 对象"
    # 生效来源会被记成 "la"（AC5 的日志级判据），状态仍为空——两者语义不同
    assert svc.last_seal_source_effective == "la"
    assert getattr(svc, "last_seal_source_status", "") == ""


def test_visual_seal_source_default_is_la() -> None:
    """默认必须是 la：门④ AC2 判定为 FAIL（生产链召回不劣化未达成）。

    本用例是「不许悄悄把默认值翻回 mineru」的机器判据——若有人改了默认，
    这里必须红，并逼其在方案/Issue 里给出新的验收依据。
    """
    from app.core.config import Settings

    assert Settings().VISUAL_SEAL_SOURCE == "la"


def test_visual_seal_source_validator_rejects_unknown_and_clamps_timeout():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(VISUAL_SEAL_SOURCE="maybe")

    assert Settings(VISUAL_SEAL_SOURCE="MINERU").VISUAL_SEAL_SOURCE == "mineru"
    assert Settings(VISUAL_SEAL_MINERU_TIMEOUT=-5).VISUAL_SEAL_MINERU_TIMEOUT == 1.0
    assert Settings(VISUAL_SEAL_MINERU_TIMEOUT=10**6).VISUAL_SEAL_MINERU_TIMEOUT == 600.0


# --------------------------------------------------------------------------
# 4. 门①.5 r6 处置新增：单位漂移与块形判定范围
# --------------------------------------------------------------------------


def test_extract_reports_unit_drift_without_silent_clamp():
    """越界 bbox 不得被静默夹到页内：判 unit 漂移并交由调用方回落（r6 C-5）。"""
    out_of_range = {"type": "image", "sub_type": "seal", "bbox": [100, 100, 1200, 200]}
    boxes_by_page, status, warnings, drifted = extract_seal_boxes([[out_of_range]])

    assert status == STATUS_OK
    assert boxes_by_page == {}
    assert drifted == frozenset({0})
    assert any("unit drift" in w for w in warnings)


def test_extract_rejects_box_whose_derived_extent_exceeds_one():
    """第二条定向单位样本：派生 y+height>1（原始 y1=1100 越出 0-1000 口径）⇒ 判单位漂移。

    与上一例（x1=1200）互为角点对照：r6 C-5 要求的两个方向（x 溢出 / y 溢出）都在
    无静默 clamp 的前提下被抓住。
    """
    beyond = {"type": "image", "sub_type": "seal", "bbox": [100, 900, 300, 1100]}
    boxes_by_page, status, warnings, drifted = extract_seal_boxes([[beyond]])

    assert status == STATUS_OK
    assert boxes_by_page == {}
    assert drifted == frozenset({0})
    assert any("unit drift" in w for w in warnings)


def test_non_candidate_blocks_do_not_trigger_shape_drift():
    """普通块（title/paragraph/page_footer/...）成群出现不得被判块形漂移（r4 R4-2）。"""
    page = [
        {"type": "title", "text": "x"},
        {"type": "paragraph", "text": "y"},
        {"type": "page_footer", "text": "1"},
        {"type": "index", "text": "z"},
    ]
    boxes_by_page, status, warnings, drifted = extract_seal_boxes([page])

    assert (boxes_by_page, status, warnings, drifted) == ({}, STATUS_OK, [], frozenset())


def test_mineru_confidence_none_survives_merge_and_presentation():
    """r6 C-8 / r7 §19.1：**链上**双断言——合并后与呈现后 `confidence` 仍为 `None`。

    不得只断言出口（`_apply_mineru_seal_source` 的返回值）：r6 问的是这条 `None`
    能不能穿过 `_merge_seal_shards`（按面积折叠 + `_higher_confidence`）与
    `_present_seals_as_visual`（改写 `source_detail`/`evidence_source`）而不被
    任何一环补成常数。
    """
    svc = _service()
    vl = BoundingBox(
        id="ocr_seal_1_abcd1234", page=1, type="official_seal", text="公章",
        x=0.15, y=0.15, width=0.1, height=0.1, confidence=0.9,
        source="visual_features", source_detail="paddleocr_vl:seal",
        evidence_source="visual_feature_model",
    )
    mineru = BoundingBox(
        id="mineru_seal_1_0_deadbeef", page=1, type="official_seal", text="公章",
        x=0.10, y=0.10, width=0.30, height=0.30, confidence=None,
        source="visual_features", source_detail="mineru_seal",
        evidence_source="visual_feature_model",
    )

    # 路径一：MinerU 框**单独存在**（无 VL 同伴）⇒ None 必须一路穿到底
    solo = svc._merge_seal_shards([mineru])
    assert [b.confidence for b in solo] == [None]
    solo_presented = VisionService._present_seals_as_visual(solo)
    assert [b.confidence for b in solo_presented] == [None]
    assert solo_presented[0].source_detail == "visual_feature_model"

    # 路径二：与 VL 框**折叠成 hull**（r7 关注的那条链）——实测行为：
    # hull 保留较大的 MinerU 框身份与几何，但 confidence 由 `_higher_confidence`
    # 取到 VL 的**已测分值 0.9**（`None` 不覆盖它）。这不是「补常数」，
    # 而是折叠继承同伴的实测分；AC6 因此**不得**表述为「任何情况下都保持 None」，
    # 必须写成「单独存在时保持 None；hull 折叠时继承同伴的实测分值」。
    merged = svc._merge_seal_shards([mineru, vl])
    seals = [b for b in merged if b.type == "official_seal"]
    assert len(seals) == 1, [b.source_detail for b in seals]
    assert seals[0].source_detail == "mineru_seal"
    assert seals[0].confidence == pytest.approx(0.9)

    presented = VisionService._present_seals_as_visual(merged)
    after = [b for b in presented if b.type == "official_seal"]
    assert len(after) == 1
    assert after[0].source_detail == "visual_feature_model"
    assert after[0].confidence == pytest.approx(0.9)
