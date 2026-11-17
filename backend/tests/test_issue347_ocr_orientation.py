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
    KEYWORD_HITS_MIN,
    OrientationUnresolvedError,
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
    """正常横向表单页：多行宽扁块、CJK 充足、表单关键词成串（ sober 判别锚）。"""
    blocks = []
    for i in range(12):
        blocks.append(_block("账号：6222021234567890 户名：李某某 交易日期：2024年3月15日 金额：3000.00",
                             0.05, 0.05 + i * 0.08, 0.6, 0.03, page_w, page_h))
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

    def test_keyword_hits_counted(self):
        # 关键词清醒判别（Issue#347 根因②演进）：表单词成串命中 vs 乱码页全灭
        upright = score_ocr_blocks(_horizontal_page_blocks(), PAGE_W, PAGE_H)
        lying = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        assert upright.signals["keyword_hits"] >= KEYWORD_HITS_MIN
        assert lying.signals["keyword_hits"] == 0

    def test_keyword_hits_ascii_case_insensitive(self):
        blocks = [_block("ACCOUNT 6222021234567890 AMOUNT 3000.00", 0.05, 0.05, 0.6, 0.03, PAGE_W, PAGE_H)]
        s = score_ocr_blocks(blocks, PAGE_W, PAGE_H)
        assert s.signals["keyword_hits"] >= 2


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

def _cand(score: float, keyword_hits: int) -> dict:
    return {"score": score, "keyword_hits": keyword_hits}


class TestChooseOrientation:
    def test_fine_page_short_circuits(self):
        as_is = score_ocr_blocks(_horizontal_page_blocks(), PAGE_W, PAGE_H)
        d = choose_orientation(as_is, {})
        assert d["angle"] == 0
        assert d["retried"] is False
        assert d["resolved"] is True

    def test_suspect_page_picks_sober_upright(self):
        as_is = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        # 模拟 rot90 候选清醒（kw=46）、rot270 倒置乱码（kw=0）
        d = choose_orientation(as_is, {90: _cand(467.0, 46), 270: _cand(350.0, 0)})
        assert d["angle"] == 90
        assert d["retried"] is True
        assert d["resolved"] is True

    def test_sober_candidate_beats_volatile_score_p10_regression(self):
        """p10 复现实证（根因②演进的核心回归钉）：批量 VL 波动把真转正候选压到
        227、倒置乱码帧抬到 344，as-is 241——比较制择优（含 1.2× 边际）全部
        指向错向/回退 ⇒ 乱码帧出框 ⇒ 表头裸露。关键词清醒判别下 90°（kw=46）
        直接胜出，分数再高的乱码帧（kw=0）也不选。"""
        from dataclasses import replace

        as_is = replace(
            score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H), score=241.0
        )
        d = choose_orientation(
            as_is,
            {90: _cand(227.0, 46), 270: _cand(344.0, 0)},
        )
        assert 227.0 < as_is.score * 1.2  # 真候选过不了比较制边际（实证条件）
        assert d["angle"] == 90
        assert d["resolved"] is True

    def test_as_is_upright_sober_keeps_zero(self):
        """as-is 有旋转签名（巨型 blob）但文本行水平且关键词清醒（原样即正，
        仅解析粗糙）：保持 0，不信任任何候选——不引入「把可读页转歪」的新风险。"""
        blob = _block("账号：6222021234567890 户名：李某某 交易日期：2024年3月15日 金额：3000.00",
                      0.02, 0.02, 0.96, 0.7, PAGE_W, PAGE_H)
        as_is = score_ocr_blocks([blob], PAGE_W, PAGE_H)
        assert as_is.rotation_suspect is True
        assert as_is.vertical_share <= 0.25
        assert as_is.keyword_hits >= KEYWORD_HITS_MIN
        d = choose_orientation(as_is, {90: _cand(500.0, 46)})
        assert d["angle"] == 0
        assert d["resolved"] is True

    def test_as_is_lying_sober_still_normalizes(self):
        """复跑实证（根因②演进第二钉）：竖躺页（vertical_share≈0.7）的 as-is
        OCR 也可能读出成串表单词（kw=44，VL 侧读），但竖排几何仍截断字符框
        （p9 证号框 h=0.053 只盖 ~11/18 位；归一页 p10 h=0.096 全盖）⇒ 竖躺
        页必须归一到直立候选，as-is kw 不构成保持原样的理由。"""
        blocks = _rotated_page_blocks()  # 竖排块（vsh=1.0）
        blocks[0] = _vertical_block("账号：6222021234567890 户名：李某某 交易日期：2024年3月15日",
                                    0.1, 0.1, 0.022, 0.12, PAGE_W, PAGE_H)
        as_is = score_ocr_blocks(blocks, PAGE_W, PAGE_H)
        assert as_is.rotation_suspect is True
        assert as_is.vertical_share > 0.4
        assert as_is.keyword_hits >= KEYWORD_HITS_MIN
        d = choose_orientation(as_is, {90: _cand(227.0, 46), 270: _cand(344.0, 0)})
        assert d["angle"] == 90  # 清醒候选胜出，不保持竖躺原样
        assert d["resolved"] is True

    def test_all_garbage_resolved_false(self):
        """无清醒候选且 as-is 旋转嫌疑 ⇒ resolved=False（调用方升级页级显式失败，
        宁可显式失败不可静默少打码）。"""
        as_is = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        d = choose_orientation(as_is, {90: _cand(344.0, 0), 270: _cand(350.0, 0)})
        assert d["angle"] == 0
        assert d["resolved"] is False

    def test_missing_keyword_key_treated_garbage(self):
        """候选字典缺 keyword_hits 键（旧调用形态）按 0 处理 = 不清醒。"""
        as_is = score_ocr_blocks(_rotated_page_blocks(), PAGE_W, PAGE_H)
        d = choose_orientation(as_is, {90: {"score": 500.0}})
        assert d["resolved"] is False

    def test_keyword_hits_min_constant(self):
        assert KEYWORD_HITS_MIN == 3


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
        assert svc.last_ocr_orientation["angle"] == 90  # 90° 候选关键词清醒 ⇒ 早退胜出
        assert svc.last_ocr_orientation["retried"] is True
        assert svc.last_ocr_orientation["resolved"] is True
        assert calls == [(400, 800), (800, 400)]  # as-is + rot90（关键词清醒早退，rot270 免跑）
        # NER/匹配吃的是胜者（横排）块
        assert svc.last_ocr_blocks[0].text.startswith("账号")
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
        assert svc.last_ocr_orientation["resolved"] is True
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

        async def fake_detect_and_draw(image_bytes, vision_types=None, draw_result=True,
                                       blocks_out=None, orientation_out=None):
            if orientation_out is not None:
                orientation_out.update({"angle": 90, "retried": True, "resolved": True, "scores": {}})
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
    async def test_mapping_uses_per_call_orientation_not_singleton(self, monkeypatch):
        """竞态安全钉（复跑实证 p33）：出口映射必须用本调用 detect 出参里的角，
        单例被并发页污染（故意设成不同角）也不得影响本页映射——否则框 180° 错位、
        遮码落空白、真值裸露。"""
        import app.services.vision_service as vs_mod
        from app.services.ocr_has_vision_service import SensitiveRegion
        from app.services.vision_service import VisionService

        vs = VisionService()
        ocr_svc = vs.ocr_has_service

        async def fake_detect_and_draw(image_bytes, vision_types=None, draw_result=True,
                                       blocks_out=None, orientation_out=None):
            # 单例被「并发页」污染成 270；本调用出参是 90——映射必须用 90
            ocr_svc.last_ocr_orientation = {"angle": 270, "retried": True, "resolved": True, "scores": {}}
            if orientation_out is not None:
                orientation_out.update({"angle": 90, "retried": True, "resolved": True, "scores": {}})
            return [SensitiveRegion(text="张三", entity_type="PERSON", left=30, top=60, width=40, height=40)], None

        monkeypatch.setattr(ocr_svc, "detect_and_draw", fake_detect_and_draw)
        monkeypatch.setattr(vs_mod, "region_has_visible_ink", lambda *a, **k: True)

        boxes, _ = await vs._detect_with_ocr_has(_png_bytes(200, 100), page=8, pipeline_types=None, draw_result=False)
        # 出参角 90：(0.3, 0.3, 0.4, 0.2) → (0.3, 0.3, 0.2, 0.4)（若误读单例 270 会得到 (0.5, 0.3, 0.2, 0.4)）
        assert boxes[0].x == pytest.approx(0.3)
        assert boxes[0].y == pytest.approx(0.3)
        assert boxes[0].width == pytest.approx(0.2)
        assert boxes[0].height == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_no_orientation_identity(self, monkeypatch):
        from app.services.ocr_has_vision_service import SensitiveRegion
        from app.services.vision_service import VisionService

        vs = VisionService()
        ocr_svc = vs.ocr_has_service

        async def fake_detect_and_draw(image_bytes, vision_types=None, draw_result=True,
                                       blocks_out=None, orientation_out=None):
            if orientation_out is not None:
                orientation_out.update({"angle": 0, "retried": False, "resolved": True, "scores": {}})
            return [SensitiveRegion(text="张三", entity_type="PERSON", left=60, top=30, width=40, height=40)], None

        monkeypatch.setattr(ocr_svc, "detect_and_draw", fake_detect_and_draw)
        import app.services.vision_service as vs_mod
        monkeypatch.setattr(vs_mod, "region_has_visible_ink", lambda *a, **k: True)
        boxes, _ = await vs._detect_with_ocr_has(_png_bytes(200, 100), page=1, pipeline_types=None, draw_result=False)
        assert boxes[0].x == pytest.approx(60 / 200)
        assert boxes[0].y == pytest.approx(30 / 100)

    @pytest.mark.asyncio
    async def test_not_confident_first_still_evaluates_second(self, monkeypatch):
        """第一个候选「结构正常但关键词不清醒」（横排乱码形态，kw=0）⇒ 早退闸
        不开、必须继续试 270；270 清醒 ⇒ 胜出。"""
        from app.services.ocr_has_vision_service import get_ocr_has_vision_service
        svc = get_ocr_has_vision_service()
        calls: list[tuple[int, int]] = []

        def _garbage_horizontal(page_w: int, page_h: int) -> list[OCRTextBlock]:
            # 横排（vertical_share=0 → clearly_fine 走 cjk 门）但无表单词：kw=0
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
        assert calls == [(400, 800), (800, 400), (800, 400)]  # 90° 乱码 kw=0 ⇒ 270° 照跑
        assert svc.last_ocr_orientation["angle"] == 270


class TestKeywordSoberGate:
    """Issue#347 根因②演进：关键词清醒判别取代比较制分数（批量 VL 波动击穿
    比较制的复现实证见 test_sober_candidate_beats_volatile_score_p10_regression）。"""

    @pytest.mark.asyncio
    async def test_garbage_candidate_still_evaluates_second(self, monkeypatch):
        """错向候选（倒置乱码，kw=0）无论结构多「正常」、分数多高，都不得早退；
        真 upright（kw≥3）在第二个候选才被评到 ⇒ 胜出。复跑 p39 实证的泛化形态
        （修前：倒置低分读数过相对边际早退选错向）。"""
        from app.services.ocr_has_vision_service import get_ocr_has_vision_service
        svc = get_ocr_has_vision_service()
        calls: list[tuple[int, int]] = []

        def _sparse_upside(page_w: int, page_h: int) -> list[OCRTextBlock]:
            # 倒置但横排、clearly_fine（cjk 40≥30）、无表单词：kw=0
            return [_block("末页汇总行数字零星", 0.08 + i * 0.16, 0.1 + (i % 2) * 0.4, 0.14, 0.035, page_w, page_h)
                    for i in range(5)]

        def fake_run_paddle_ocr(image, require_visual_regions=False, selected_entity_types=None, stage_status=None):
            calls.append((image.width, image.height))
            if image.width < image.height:
                # as-is：稀疏竖排（suspect、kw=0）
                blocks = [_vertical_block("李某某20100424自助", 0.1 + i * 0.2, 0.1, 0.02, 0.1, image.width, image.height)
                          for i in range(6)]
                return blocks, []
            if len(calls) == 2:
                return _sparse_upside(image.width, image.height), []
            # 270 候选：真 upright（表单词清醒）
            return _horizontal_page_blocks(image.width, image.height), []

        async def fake_ner(blocks, vision_types=None, stage_status=None, page_deadline=None):
            return []

        monkeypatch.setattr(svc, "_run_paddle_ocr", fake_run_paddle_ocr)
        monkeypatch.setattr(svc, "_invoke_has_text_analysis", fake_ner)
        monkeypatch.setattr(svc, "_apply_regex_fallback", lambda *a, **k: [])
        await svc.detect_and_draw(_png_bytes(400, 800), vision_types=None, draw_result=False)
        assert calls == [(400, 800), (800, 400), (800, 400)]  # 90° kw=0 ⇒ 不早退，270° 照评
        assert svc.last_ocr_orientation["angle"] == 270
        assert svc.last_ocr_orientation["resolved"] is True

    @pytest.mark.asyncio
    async def test_sober_candidate_early_exits(self, monkeypatch):
        """关键词清醒的候选（kw≥3 且结构正常）⇒ 早退，省第二次纯 OCR。"""
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
        assert calls == [(400, 800), (800, 400)]  # 90° 清醒 ⇒ 照常早退
        assert svc.last_ocr_orientation["angle"] == 90

    @pytest.mark.asyncio
    async def test_all_garbage_raises_unresolved(self, monkeypatch):
        """所有方向都不清醒（候选 OCR 乱码/关键词全灭）⇒ OrientationUnresolvedError
        从 detect_and_draw 上抛（vision_service 按 #199 语义升级页级显式失败）。"""
        from app.services.ocr_has_vision_service import get_ocr_has_vision_service
        svc = get_ocr_has_vision_service()

        def _garbage_horizontal(page_w: int, page_h: int) -> list[OCRTextBlock]:
            return [_block("杂读碎符碎片乱块文本", 0.05 + i * 0.18, 0.1 + (i % 2) * 0.5, 0.15, 0.04, page_w, page_h)
                    for i in range(5)]

        def fake_run_paddle_ocr(image, require_visual_regions=False, selected_entity_types=None, stage_status=None):
            if image.width < image.height:
                return _rotated_page_blocks(image.width, image.height), []  # as-is 竖排 kw=0
            return _garbage_horizontal(image.width, image.height), []      # 候选横排乱码 kw=0

        async def fake_ner(blocks, vision_types=None, stage_status=None, page_deadline=None):
            return []

        monkeypatch.setattr(svc, "_run_paddle_ocr", fake_run_paddle_ocr)
        monkeypatch.setattr(svc, "_invoke_has_text_analysis", fake_ner)
        monkeypatch.setattr(svc, "_apply_regex_fallback", lambda *a, **k: [])
        with pytest.raises(OrientationUnresolvedError, match="orientation unresolved"):
            await svc.detect_and_draw(_png_bytes(400, 800), vision_types=None, draw_result=False)
        # 观测先行落盘（失败页的 stage_duration_ms 仍能读到 ocr_orientation）
        assert svc.last_ocr_orientation["resolved"] is False
        assert svc.last_ocr_orientation["angle"] == 0


class TestOrientationFatalWiring:
    """页级失败接线钉：is_page_fatal_stage_error 必须认 OrientationUnresolvedError
    （否则方向未决被降级为 stage warning、页照常 completed = 静默漏打放行）。"""

    def test_unresolved_is_page_fatal(self):
        from app.services.vision_service import is_page_fatal_stage_error

        assert is_page_fatal_stage_error(OrientationUnresolvedError("x")) is True

    def test_plain_runtime_error_stays_non_fatal(self):
        # 普通 RuntimeError（如一般 OCR 抖动）维持「stage failed + 页 completed」
        from app.services.vision_service import is_page_fatal_stage_error

        assert is_page_fatal_stage_error(RuntimeError("boom")) is False
