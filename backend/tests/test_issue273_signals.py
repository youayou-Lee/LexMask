"""Issue#273 Task 5：L1/L2 页级信号采集器（signals.py）测试。

合成 PDF 一律用 fitz 现造（tmp_path）。中文字符必须用内置 CJK 字体 ``china-s``
写入：默认 helv 不含 CJK 字形，实测中文会退化成一串 ``·``，令
``page_repeat_ratio < 0.3`` 断言必然失败——这是对需求书测试夹具的必要环境修正，
其余测试代码逐字保留（唯一例外：末条 memo 测试按控制器裁定改写断言）。
"""

import fitz

from app.services.page_routing import signals


def _make_pdf(tmp_path, pages: list[list[str]], *, image_half=None) -> str:
    path = str(tmp_path / "synthetic.pdf")
    doc = fitz.open()
    for words in pages:
        page = doc.new_page()
        y = 72
        for w in words:
            # 默认 helv 无 CJK 字形；内置 china-s 才能把中文真正落进文本层。
            page.insert_text((72, y), w, fontsize=12, fontname="china-s")
            y += 20
        if image_half is not None:
            rect = fitz.Rect(0, page.rect.height / 2, page.rect.width, page.rect.height)
            page.insert_image(rect, pixmap=image_half)
    doc.save(path)
    return path


def test_collect_l1_normal_text_page(tmp_path):
    path = _make_pdf(tmp_path, [["中华人民共和国最高人民法院", "二〇二四年三月十五日"] * 3])
    s = signals.collect_l1(path, 1)
    assert s.probe_error is False and s.chars > 20 and s.garbage_ratio == 0
    assert s.page_repeat_ratio < 0.3 and s.is_pdf is True


def test_collect_l1_sparse_page(tmp_path):
    path = _make_pdf(tmp_path, [["百色市院"]])  # 4 字水印串，重复
    s = signals.collect_l1(path, 1)
    assert s.chars < 120  # 判 ocr 的输入形态


def test_collect_l1_missing_page_is_probe_error(tmp_path):
    path = _make_pdf(tmp_path, [["正常文本内容一段"]])
    s = signals.collect_l1(path, 99)
    assert s.probe_error is True and s.chars == 0


def test_collect_l1_nonexistent_file_is_probe_error(tmp_path):
    s = signals.collect_l1(str(tmp_path / "nope.pdf"), 1)
    assert s.probe_error is True


def test_collect_l1_corrupt_file_is_probe_error(tmp_path):
    # 约束 2：损坏 PDF 也必须 probe_error=True，绝不抛。
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not a pdf at all")
    s = signals.collect_l1(str(bad), 1)
    assert s.probe_error is True and s.chars == 0


def test_collect_l1_encrypted_without_password_is_probe_error(tmp_path):
    # 约束 2：加密无密码 ⇒ probe_error=True，绝不抛。
    enc = tmp_path / "enc.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "加密正文内容", fontname="china-s")
    doc.save(str(enc), encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    doc.close()
    s = signals.collect_l1(str(enc), 1)
    assert s.probe_error is True and s.chars == 0


def test_collect_l2_page(tmp_path):
    path = _make_pdf(tmp_path, [["人民法院判决书正文若干字"] * 5])
    ink, coverage = signals.collect_l2(path, 1, dpi=100)
    # fitz insert_text 是真墨迹 ⇒ ink 高；无整页图 ⇒ coverage 低
    assert ink is not None and ink > 0.02
    assert coverage is not None and coverage < 0.5


def test_collect_l2_missing_page_returns_none_none(tmp_path):
    path = _make_pdf(tmp_path, [["正文"]])
    assert signals.collect_l2(path, 42, dpi=100) == (None, None)


def test_collect_l2_image_coverage(tmp_path):
    # 需求书夹具的 image_half 通道：下半页放一张真 pixmap ⇒ image_coverage 显著非零。
    # insert_image 默认保比例放置，60x40 塞进半页 ⇒ 实测 bbox 高约 0.47×页高。
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40))
    pix.set_rect(pix.irect, (180, 40, 40))
    path = _make_pdf(tmp_path, [["正文一段"]], image_half=pix)
    ink, coverage = signals.collect_l2(path, 1, dpi=100)
    assert ink is not None and ink > 0.02
    assert coverage is not None and 0.3 < coverage < 0.7


def test_collect_l1_fullpage_image_with_text_layer(tmp_path):
    # 「整页图+内嵌文本层」（扫描页 OCR 产物层的典型形态）：coverage 必须在 L1
    # 即可采到——判定序规则 5.5（judge 侧）以 collect_l1 的 image_coverage 为输入，
    # 不能再等灰区才触发的 L2 补采。夹具=先渲染文字页 pixmap（模拟扫描成像），
    # 正式页 insert_text 文本层 + 整页矩形 insert_image 贴图。
    scratch = fitz.open()
    spage = scratch.new_page()
    y = 72
    for line in ["人民法院判决书正文"] * 8:
        spage.insert_text((72, y), line, fontsize=12, fontname="china-s")
        y += 20
    pix = spage.get_pixmap(dpi=150)
    scratch.close()
    path = str(tmp_path / "fullpage_image.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "人民法院判决书正文", fontsize=12, fontname="china-s")
    page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    s = signals.collect_l1(path, 1)
    assert s.probe_error is False
    assert s.image_coverage is not None and s.image_coverage >= 0.9


def test_collect_l1_rotated_fullpage_image_with_text_layer(tmp_path):
    # 门⑥ I-1：90° 旋转页缝隙——旧口径「图 bbox 与页 rect 剪裁交面积和/页面积」在
    # 旋转页上（bbox 与旋转后页 rect 错位，交集只剩 ~0.7）漏过规则 5.5，而执行层
    # _is_scanned_page 用未剪裁 abs(rect) 仍判扫描 ⇒ 两闸缝隙重开。routing 侧谓词
    # 对齐执行层后旋转不变：整页图+内嵌层+页级 /Rotate 90 必须 image_coverage ≥0.9。
    scratch = fitz.open()
    spage = scratch.new_page()
    y = 72
    for line in ["人民法院判决书正文"] * 8:
        spage.insert_text((72, y), line, fontsize=12, fontname="china-s")
        y += 20
    pix = spage.get_pixmap(dpi=150)
    scratch.close()
    path = str(tmp_path / "rotated_fullpage_image.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "人民法院判决书正文", fontsize=12, fontname="china-s")
    page.insert_image(page.rect, pixmap=pix)
    page.set_rotation(90)  # 扫描仪常写的页级旋转元数据
    doc.save(path)
    s = signals.collect_l1(path, 1)
    assert s.probe_error is False
    assert s.image_coverage is not None and s.image_coverage >= 0.9


def test_l1_memo_same_object(tmp_path):
    # 需求书原断言引用不存在的属性（s1.signals_dict_key）且三元恒真（已知瑕疵）；
    # 按其注释意图改写：两次采集的关键数值完全一致 ⇒ memo 生效（同文件只 probe 一次）。
    path = _make_pdf(tmp_path, [["法院判决书正文"] * 8])
    s1 = signals.collect_l1(path, 1)
    s2 = signals.collect_l1(path, 1)
    assert s1.chars == s2.chars
    assert s1.garbage_ratio == s2.garbage_ratio
    assert s1.page_repeat_ratio == s2.page_repeat_ratio
