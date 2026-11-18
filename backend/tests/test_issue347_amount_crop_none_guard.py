"""Issue#347 修 B：金额紧裁（Issue#308 收缩路径）的 None 防御回归。

`_crop_amount_block_region` 拿 `_entity_char_box_line_rects` 的 None 直接
len() → TypeError 炸掉整个 ocr_has 阶段 → 整页 fail-open 0 框（修前 A-p8/
A-p14 整页未打的直接根因）。契约：None ⇒ 按收不出回退整块罩（never-uncover
不变），与同函数其他调用点口径一致。
"""


from app.services.ocr_has_vision_service import OCRTextBlock

# ---------------------------------------------------------------- 金额紧裁 None 防御（修 B）

class TestCropAmountBlockRegionNoneGuard:
    def _amount_block(self, chars) -> OCRTextBlock:
        return OCRTextBlock(
            text="金额：1431400.00元整",
            polygon=[[0, 0], [300, 0], [300, 40], [0, 40]],
            chars=chars,
        )

    def test_char_rects_none_returns_none_not_crash(self, monkeypatch):
        from app.services.vision import ocr_entity_match as m
        # 契约允许 _entity_char_box_line_rects 返回 None——必须按"收不出"回退整块，
        # 而不是 len(None) 崩溃（修前：整页 ocr_has 阶段炸掉、fail-open 0 框）。
        # chars 必须非空，才能穿过「无 chars 早退」分支真正走到防御点。
        text = "金额：1431400.00元整"
        chars = [{"c": ch, "x1": float(i * 12), "y1": 0.0, "x2": float(i * 12 + 12), "y2": 24.0}
                 for i, ch in enumerate(text)]
        monkeypatch.setattr(m, "_entity_char_box_line_rects", lambda *a, **k: None)
        out = m._crop_amount_block_region(self._amount_block(chars), text, "1431400.00", 30.0)
        assert out is None

    def test_zero_width_char_boxes_return_none_not_crash(self):
        from app.services.vision import ocr_entity_match as m
        # 几何真实形态：旋转页竖排字符框零宽 → rects 全被宽过滤 → None → 回退整块
        text = "金额：1431400.00元整"
        chars = [
            {"c": ch, "x1": 10.0, "y1": float(i * 12), "x2": 10.0, "y2": float(i * 12 + 12)}
            for i, ch in enumerate(text)
        ]
        out = m._crop_amount_block_region(self._amount_block(chars), text, "1431400.00", 30.0)
        assert out is None

    def test_normal_single_run_still_crops(self):
        from app.services.vision import ocr_entity_match as m
        # 回归保护：正常横排块仍然紧裁（修 A/修 B 都不能破坏 Issue#308 行为）
        text = "金额：1431400.00元整"
        chars = []
        for i, ch in enumerate(text):
            chars.append({"c": ch, "x1": float(i * 12), "y1": 0.0, "x2": float(i * 12 + 12), "y2": 24.0})
        out = m._crop_amount_block_region(self._amount_block(chars), text, "1431400.00", 30.0)
        assert out is not None
        assert out.entity_type == "AMOUNT"
        assert out.width > 0 and out.height > 0


