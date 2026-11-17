"""Issue#347：扫描页 ±90° 旋转方向归一 + 金额紧裁 None 防御。

真卷实证（6号案卷对账单，见 Issue #347 与 10 组纯 OCR 块统计夹具）：
- 双面扫描奇偶页交替 ±90° 躺倒、PDF 无 /Rotate；
- 横躺输入下 vertical_share 0.703–0.733，转正后 0.0，完美判别；
- 转正后 p8 表头字段（证号/户名/账号）全部出框。
"""

import pytest
from PIL import Image

from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.ocr_orientation import (
    WINNER_MARGIN_RATIO,
    choose_orientation,
    map_bounding_box_to_page,
    map_normalized_box_to_page,
    map_normalized_box_to_upright,
    rotate_image_for_ocr,
    score_ocr_blocks,
)


# ---------------------------------------------------------------- 合成块工具

def _block(text: str, x: float, y: float, w: float, h: float, page_w: int, page_h: int) -> OCRTextBlock:
    """横排文本块（宽 > 高）。"""
    return OCRTextBlock(
        text=text,
        polygon=[[x * page_w, y * page_h], [(x + w) * page_w, y * page_h],
                 [(x + w) * page_w, (y + h) * page_h], [x * page_w, (y + h) * page_h]],
    )


def _vertical_block(text: str, x: float, y: float, w: float, h: float, page_w: int, page_h: int) -> OCRTextBlock:
    """竖排文本块（高 > 宽×1.8）——横躺页的典型形态。"""
    return OCRTextBlock(
        text=text,
        polygon=[[x * page_w, y * page_h], [(x + w) * page_w, y * page_h],
                 [(x + w) * page_w, (y + h) * page_h], [x * page_w, (y + h) * page_h]],
    )


PAGE_W, PAGE_H = 2584, 3655


def _horizontal_page_blocks(page_w: int = PAGE_W, page_h: int = PAGE_H) -> list[OCRTextBlock]:
    """正常横向页：多行宽扁块、CJK 充足。"""
    blocks = []
    for i in range(12):
        blocks.append(_block("合同编号：二〇二四年三月签订于南宁市青秀区", 0.05, 0.05 + i * 0.08, 0.6, 0.03, page_w, page_h))
    return blocks


def _rotated_page_blocks(page_w: int = PAGE_W, page_h: int = PAGE_H) -> list[OCRTextBlock]:
    """横躺页：大量竖排窄高块（模拟 ±90° 扫描页的 OCR 输出）。"""
    blocks = []
    for i in range(20):
        blocks.append(_vertical_block("李某某20100424自助设备现金3000.00", 0.1 + i * 0.04, 0.1, 0.022, 0.12, page_w, page_h))
    return blocks


# ---------------------------------------------------------------- 评分器

class TestScoreOcrBlocks:
    def test_horizontal_clean_page_is_clearly_fine(self):
        s = score_ocr_blocks(_horizontal_page_blocks(), PAGE_W, PAGE_H)
        assert s.clearly_fine is True
        assert s.rotation_suspect is False
        assert s.signals["vertical_share"] <= 0.25

    def test_rotated_page_flags_rotation(self):
        s = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        assert s.rotation_suspect is True
        assert s.clearly_fine is False
        assert s.signals["vertical_share"] > 0.4

    def test_giant_blob_flags_rotation(self):
        blob = _block("整页表格被并成一个巨型块" * 20, 0.02, 0.02, 0.96, 0.7, PAGE_W, PAGE_H)
        s = score_ocr_blocks([blob], PAGE_W, PAGE_H)
        assert s.rotation_suspect is True
        assert s.signals["blob_area"] > 0.5

    def test_near_blank_page_no_rotation_signal(self):
        # 近空白页（如封面角标 + 水印零星读取）：低文本、无竖排、无 blob
        blocks = [_block("2020年7月10日", 0.7, 0.4, 0.06, 0.01, PAGE_W, PAGE_H)]
        s = score_ocr_blocks(blocks, PAGE_W, PAGE_H)
        assert s.rotation_suspect is False  # 不触发重试
        assert s.clearly_fine is False

    def test_score_prefers_upright_over_lying_page(self):
        # 真卷夹具特征复刻：横躺 201CJK/33游程/0.729竖 vs 转正 383CJK/42游程/0 竖
        lying = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        upright = score_ocr_blocks(_horizontal_page_blocks(), PAGE_W, PAGE_H)
        assert upright.score > lying.score


# ---------------------------------------------------------------- 坐标映射

class TestMapNormalizedBox:
    def test_roundtrip_90(self):
        # 非方页 + 非方框：page (0.1, 0.2, 0.3, 0.4) → upright → 回到 page 恒等
        box = (0.1, 0.2, 0.3, 0.4)
        up = map_normalized_box_to_upright(box, 90)
        back = map_normalized_box_to_page(up, 90)
        for a, b in zip(box, back):
            assert a == pytest.approx(b, abs=1e-9)

    def test_roundtrip_270(self):
        box = (0.15, 0.25, 0.05, 0.6)
        up = map_normalized_box_to_upright(box, 270)
        back = map_normalized_box_to_page(up, 270)
        for a, b in zip(box, back):
            assert a == pytest.approx(b, abs=1e-9)

    def test_known_point_rot90_id_card(self):
        # 真卷数值锚：p8 证件号码框在转正(rot90)空间 (1866,200,371x51)/ (3655,2584)
        # → 原页归一化 (0.0774, 0.38796, 0.01974, 0.10151)（spike 数值+画框目检双验证）
        up = (1866 / 3655, 200 / 2584, 371 / 3655, 51 / 2584)
        page = map_normalized_box_to_page(up, 90)
        assert page[0] == pytest.approx(0.0774, abs=1e-4)
        assert page[1] == pytest.approx(0.38796, abs=1e-4)
        assert page[2] == pytest.approx(51 / 2584, abs=1e-6)
        assert page[3] == pytest.approx(371 / 3655, abs=1e-6)

    def test_rot90_vertical_text_stays_vertical(self):
        # 转正空间的横长条（一行字）映射回原页后必须是竖长条（w/h 互换）
        up = (0.5, 0.1, 0.4, 0.05)  # 横向文本行
        page = map_normalized_box_to_page(up, 90)
        assert page[2] == pytest.approx(0.05)
        assert page[3] == pytest.approx(0.4)


class TestRotateImageForOcr:
    def _pixel(self, img, x, y):
        return img.getpixel((x, y))

    def test_rot90_is_clockwise(self):
        # 4x2 图：左上红、右上蓝、左下绿。顺时针 90°：左上→右上、右上→右下、左下→左上
        img = Image.new("RGB", (4, 2))
        img.putpixel((0, 0), (255, 0, 0))
        img.putpixel((3, 0), (0, 0, 255))
        img.putpixel((0, 1), (0, 255, 0))
        out = rotate_image_for_ocr(img, 90)
        assert out.size == (2, 4)
        assert self._pixel(out, 1, 0) == (255, 0, 0)   # 红→右上
        assert self._pixel(out, 1, 3) == (0, 0, 255)   # 蓝→右下
        assert self._pixel(out, 0, 0) == (0, 255, 0)   # 绿→左上

    def test_rot270_is_counterclockwise(self):
        img = Image.new("RGB", (4, 2))
        img.putpixel((0, 0), (255, 0, 0))
        img.putpixel((3, 0), (0, 0, 255))
        img.putpixel((0, 1), (0, 255, 0))
        out = rotate_image_for_ocr(img, 270)
        assert out.size == (2, 4)
        assert self._pixel(out, 0, 3) == (255, 0, 0)   # 红→左下
        assert self._pixel(out, 0, 0) == (0, 0, 255)   # 蓝→左上
        assert self._pixel(out, 1, 3) == (0, 255, 0)   # 绿→右下

    def test_rot0_identity(self):
        img = Image.new("RGB", (3, 5))
        out = rotate_image_for_ocr(img, 0)
        assert out.size == (3, 5)


# ---------------------------------------------------------------- 方向决策

class TestChooseOrientation:
    def test_fine_page_short_circuits(self):
        as_is = score_ocr_blocks(_horizontal_page_blocks(), PAGE_W, PAGE_H)
        d = choose_orientation(as_is, {})
        assert d["angle"] == 0
        assert d["retried"] is False

    def test_suspect_page_picks_best_upright(self):
        as_is = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        # 模拟 rot90 候选分数更高
        d = choose_orientation(as_is, {90: 467.0, 270: 350.0})
        assert d["angle"] == 90
        assert d["retried"] is True

    def test_margin_fallback_keeps_as_is(self):
        as_is = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        # 候选仅高出 5%（< WINNER_MARGIN_RATIO）：宁可保持原样，不引入旋转错位风险
        d = choose_orientation(as_is, {90: as_is.score * 1.05, 270: as_is.score * 1.02})
        assert d["angle"] == 0
        assert d["retried"] is True

    def test_wrong_rotation_loses_to_as_is(self):
        # 错向（180°）候选分数低于横躺 as-is：必须回退 0
        as_is = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        d = choose_orientation(as_is, {90: as_is.score * 0.5, 270: as_is.score * 0.6})
        assert d["angle"] == 0

    def test_margin_constant(self):
        assert WINNER_MARGIN_RATIO == 1.2

    def test_early_exit_floor_importable(self):
        from app.services.vision.ocr_orientation import ORIENTATION_EARLY_EXIT_FLOOR
        assert ORIENTATION_EARLY_EXIT_FLOOR == 200.0


# ---------------------------------------------------------------- 映射接线（BoundingBox 层）

class TestMapBoundingBoxToPage:
    def test_identity_at_zero(self):
        from app.models.entity_schemas import BoundingBox
        box = BoundingBox(id="t1", x=0.1, y=0.2, width=0.3, height=0.4, type="PERSON", page=8)
        out = map_bounding_box_to_page(box, 0)
        assert (out.x, out.y, out.width, out.height) == (0.1, 0.2, 0.3, 0.4)

    def test_swap_dims_at_90(self):
        from app.models.entity_schemas import BoundingBox
        box = BoundingBox(id="t2", x=0.5, y=0.1, width=0.4, height=0.05, type="ID_CARD", page=8)
        out = map_bounding_box_to_page(box, 90)
        assert out.width == pytest.approx(0.05)
        assert out.height == pytest.approx(0.4)
        assert out.page == 8
        assert out.type == "ID_CARD"


# ---------------------------------------------------------------- 接线钉（detect_and_draw 方向择优 + 出口映射）

def _png_bytes(w: int, h: int) -> bytes:
    import io
    img = Image.new("RGB", (w, h), (40, 40, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestDetectAndDrawOrientationSeam:
    @pytest.mark.asyncio
    async def test_suspect_as_is_switches_to_upright(self, monkeypatch):
        """横躺签名页：as-is 竖排块 → 触发重试 → 择优转正 → NER 吃胜者块。"""
        from app.services.ocr_has_vision_service import get_ocr_has_vision_service
        svc = get_ocr_has_vision_service()
        calls: list[tuple[int, int]] = []

        def fake_run_paddle_ocr(image, require_visual_regions=False, selected_entity_types=None, stage_status=None):
            calls.append((image.width, image.height))
            if image.width < image.height:
                # as-is（竖页）：竖排窄高块 = 旋转签名
                return _rotated_page_blocks(image.width, image.height), []
            # 转正（横页）：横排宽扁块
            return _horizontal_page_blocks(image.width, image.height), []

        async def fake_ner(blocks, vision_types=None, stage_status=None, page_deadline=None):
            return []

        monkeypatch.setattr(svc, "_run_paddle_ocr", fake_run_paddle_ocr)
        monkeypatch.setattr(svc, "_invoke_has_text_analysis", fake_ner)
        monkeypatch.setattr(svc, "_apply_regex_fallback", lambda *a, **k: [])
        regions, _ = await svc.detect_and_draw(_png_bytes(400, 800), vision_types=None, draw_result=False)
        assert svc.last_ocr_orientation["angle"] == 90  # 并列时取先试的 90
        assert svc.last_ocr_orientation["retried"] is True
        assert calls == [(400, 800), (800, 400)]  # as-is + rot90（置信早退，rot270 免跑）
        # NER/匹配吃的是胜者（横排）块
        assert svc.last_ocr_blocks[0].text.startswith("合同")
        assert regions == []

    @pytest.mark.asyncio
    async def test_fine_page_never_retries(self, monkeypatch):
        from app.services.ocr_has_vision_service import get_ocr_has_vision_service
        svc = get_ocr_has_vision_service()
        calls: list[tuple[int, int]] = []

        def fake_run_paddle_ocr(image, require_visual_regions=False, selected_entity_types=None, stage_status=None):
            calls.append((image.width, image.height))
            return _horizontal_page_blocks(image.width, image.height), []

        async def fake_ner(blocks, vision_types=None, stage_status=None, page_deadline=None):
            return []

        monkeypatch.setattr(svc, "_run_paddle_ocr", fake_run_paddle_ocr)
        monkeypatch.setattr(svc, "_invoke_has_text_analysis", fake_ner)
        monkeypatch.setattr(svc, "_apply_regex_fallback", lambda *a, **k: [])
        await svc.detect_and_draw(_png_bytes(800, 400), vision_types=None, draw_result=False)
        assert svc.last_ocr_orientation["angle"] == 0
        assert svc.last_ocr_orientation["retried"] is False
        assert calls == [(800, 400)]  # 好页零额外 OCR 开销


class TestDetectWithOcrHasExitMapping:
    @pytest.mark.asyncio
    async def test_boxes_mapped_back_to_page_space(self, monkeypatch):
        """方向归一后：ink 检查在胜者图上做，落库框必须映射回原页坐标。"""
        import app.services.vision_service as vs_mod
        from app.services.ocr_has_vision_service import SensitiveRegion
        from app.services.vision_service import VisionService

        vs = VisionService()
        ocr_svc = vs.ocr_has_service

        async def fake_detect_and_draw(image_bytes, vision_types=None, draw_result=True, blocks_out=None):
            ocr_svc.last_ocr_orientation = {"angle": 90, "retried": True, "scores": {}}
            # 胜者空间（100x200 转正图）像素框
            return [SensitiveRegion(text="张三", entity_type="PERSON", left=30, top=60, width=40, height=40)], None

        monkeypatch.setattr(ocr_svc, "detect_and_draw", fake_detect_and_draw)
        monkeypatch.setattr(vs_mod, "region_has_visible_ink", lambda *a, **k: True)

        # 原页 200x100；转 90° 后 100x200
        boxes, _ = await vs._detect_with_ocr_has(_png_bytes(200, 100), page=8, pipeline_types=None, draw_result=False)
        assert len(boxes) == 1
        # 胜者空间归一化 (0.3, 0.3, 0.4, 0.2) → 原页 (0.3, 0.3, 0.2, 0.4)
        assert boxes[0].x == pytest.approx(0.3)
        assert boxes[0].y == pytest.approx(0.3)
        assert boxes[0].width == pytest.approx(0.2)
        assert boxes[0].height == pytest.approx(0.4)
        assert boxes[0].page == 8
        assert boxes[0].type == "PERSON"

    @pytest.mark.asyncio
    async def test_no_orientation_identity(self, monkeypatch):
        from app.services.ocr_has_vision_service import SensitiveRegion
        from app.services.vision_service import VisionService

        vs = VisionService()
        ocr_svc = vs.ocr_has_service

        async def fake_detect_and_draw(image_bytes, vision_types=None, draw_result=True, blocks_out=None):
            ocr_svc.last_ocr_orientation = {"angle": 0, "retried": False, "scores": {}}
            return [SensitiveRegion(text="张三", entity_type="PERSON", left=60, top=30, width=40, height=40)], None

        monkeypatch.setattr(ocr_svc, "detect_and_draw", fake_detect_and_draw)
        import app.services.vision_service as vs_mod
        monkeypatch.setattr(vs_mod, "region_has_visible_ink", lambda *a, **k: True)
        boxes, _ = await vs._detect_with_ocr_has(_png_bytes(200, 100), page=1, pipeline_types=None, draw_result=False)
        assert boxes[0].x == pytest.approx(60 / 200)
        assert boxes[0].y == pytest.approx(30 / 100)

    @pytest.mark.asyncio
    async def test_not_confident_first_still_evaluates_second(self, monkeypatch):
        """第一个候选「明显正常但分数不达边际」（横排乱码形态）⇒ 必须继续试 270。"""
        from app.services.ocr_has_vision_service import get_ocr_has_vision_service
        svc = get_ocr_has_vision_service()
        calls: list[tuple[int, int]] = []

        def _garbage_horizontal(page_w: int, page_h: int) -> list[OCRTextBlock]:
            # 横排（vertical_share=0 → clearly_fine 走 cjk 门）但文本稀碎、分数低：
            # 5 块 × 8 CJK = 40 ≥ 30 → clearly_fine True；score 40 < as-is 70×1.2
            return [_block("杂读碎符碎片乱块文本", 0.05 + i * 0.18, 0.1 + (i % 2) * 0.5, 0.15, 0.04, page_w, page_h)
                    for i in range(5)]

        def fake_run_paddle_ocr(image, require_visual_regions=False, selected_entity_types=None, stage_status=None):
            calls.append((image.width, image.height))
            if image.width < image.height:
                return _rotated_page_blocks(image.width, image.height), []
            if len(calls) == 2:
                return _garbage_horizontal(image.width, image.height), []
            return _horizontal_page_blocks(image.width, image.height), []

        async def fake_ner(blocks, vision_types=None, stage_status=None, page_deadline=None):
            return []

        monkeypatch.setattr(svc, "_run_paddle_ocr", fake_run_paddle_ocr)
        monkeypatch.setattr(svc, "_invoke_has_text_analysis", fake_ner)
        monkeypatch.setattr(svc, "_apply_regex_fallback", lambda *a, **k: [])
        await svc.detect_and_draw(_png_bytes(400, 800), vision_types=None, draw_result=False)
        assert calls == [(400, 800), (800, 400), (800, 400)]  # 90° 不达边际 ⇒ 270° 照跑
        assert svc.last_ocr_orientation["angle"] == 270


class TestEarlyExitFloor:
    """Issue#347 复跑实证：稀疏页（as-is 64）倒置读数 108 过相对边际选错向，
    户名/账号漏打。早退须同时过绝对下限（胜者分布 294–463，空隙取 200）。"""

    @pytest.mark.asyncio
    async def test_low_score_confident_candidate_still_evaluates_second(self, monkeypatch):
        from app.services.ocr_has_vision_service import get_ocr_has_vision_service
        svc = get_ocr_has_vision_service()
        calls: list[tuple[int, int]] = []

        def _sparse_upside(page_w: int, page_h: int) -> list[OCRTextBlock]:
            # 倒置但横排、clearly_fine（cjk 40≥30）、score 108 < 200 下限
            return [_block("末页汇总行数字零星", 0.08 + i * 0.16, 0.1 + (i % 2) * 0.4, 0.14, 0.035, page_w, page_h)
                    for i in range(5)]

        def fake_run_paddle_ocr(image, require_visual_regions=False, selected_entity_types=None, stage_status=None):
            calls.append((image.width, image.height))
            if image.width < image.height:
                # as-is：稀疏竖排（suspect）
                blocks = [_vertical_block("李某某20100424自助", 0.1 + i * 0.2, 0.1, 0.02, 0.1, image.width, image.height)
                          for i in range(6)]
                return blocks, []
            if len(calls) == 2:
                return _sparse_upside(image.width, image.height), []
            # 270 候选：真 upright，分数更高
            return _horizontal_page_blocks(image.width, image.height), []

        async def fake_ner(blocks, vision_types=None, stage_status=None, page_deadline=None):
            return []

        monkeypatch.setattr(svc, "_run_paddle_ocr", fake_run_paddle_ocr)
        monkeypatch.setattr(svc, "_invoke_has_text_analysis", fake_ner)
        monkeypatch.setattr(svc, "_apply_regex_fallback", lambda *a, **k: [])
        await svc.detect_and_draw(_png_bytes(400, 800), vision_types=None, draw_result=False)
        assert calls == [(400, 800), (800, 400), (800, 400)]  # 108 < 200 ⇒ 不早退，270 照评
        assert svc.last_ocr_orientation["angle"] == 270

    @pytest.mark.asyncio
    async def test_strong_confident_candidate_still_early_exits(self, monkeypatch):
        from app.services.ocr_has_vision_service import get_ocr_has_vision_service
        svc = get_ocr_has_vision_service()
        calls: list[tuple[int, int]] = []

        def fake_run_paddle_ocr(image, require_visual_regions=False, selected_entity_types=None, stage_status=None):
            calls.append((image.width, image.height))
            if image.width < image.height:
                return _rotated_page_blocks(image.width, image.height), []
            return _horizontal_page_blocks(image.width, image.height), []

        async def fake_ner(blocks, vision_types=None, stage_status=None, page_deadline=None):
            return []

        monkeypatch.setattr(svc, "_run_paddle_ocr", fake_run_paddle_ocr)
        monkeypatch.setattr(svc, "_invoke_has_text_analysis", fake_ner)
        monkeypatch.setattr(svc, "_apply_regex_fallback", lambda *a, **k: [])
        await svc.detect_and_draw(_png_bytes(400, 800), vision_types=None, draw_result=False)
        assert calls == [(400, 800), (800, 400)]  # 450 ≥ 200 ⇒ 照常早退
        assert svc.last_ocr_orientation["angle"] == 90
