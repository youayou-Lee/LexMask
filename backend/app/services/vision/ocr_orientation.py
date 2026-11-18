"""Issue#347：扫描页 ±90° 方向归一（try-and-pick）——纯函数层。

真卷实证（6号案卷对账单，Issue #347）：双面扫描奇偶页交替 ±90° 躺倒、PDF 无
/Rotate 标志，OCR 服务端方向分类全关，横躺输入让 PaddleOCR-VL 退化（0 框 /
巨型表格 blob / 乱码），表头字段读不出 → 打码整行漏打；竖排文本行在「横排行」
假设的字符框逻辑下被截断 → 证件号只遮前段。

修法：OCR 前不假设方向——as-is 块评分，出现「旋转签名」（竖排行块占比高或
巨型 blob）时把页图转 90°/270° 各补一次纯 OCR，按「关键词清醒判别」择优；
胜者空间做 NER/匹配，归一化框在 `_detect_with_ocr_has` 出口单点映射回原页
坐标。好页零额外开销（不触发重试），坏页不浪费 NER（重试只跑 OCR）。

判别器演进（Issue#347 复跑实证）：比较制分数择优被批量 VL 输出波动击穿——
同一张图三次 OCR 评分 450/463/450，批内真转正候选被压低到 227、倒置乱码
帧反超到 344，as-is 241×1.2 边际同时压死真候选 ⇒ 错向帧出框 ⇒ NER 锚点
错乱、表头裸露。表单关键词命中数对 VL 波动免疫（真 upright kw=46 vs
as-is/错向 kw=0），取代分数成为决定性判别器；所有候选都不清醒 ⇒
`OrientationUnresolvedError` 页级显式失败（#199 语义，宁可显式失败不可
静默少打码）。竖躺页（vertical_share>0.4）即使 as-is 关键词清醒也必须归一
——侧读能读出词但竖排几何截断字符框（复跑实证：保持原样的奇数页证号框只盖
11/18 位，归一页全盖）。

方向语义（与 PyMuPDF `page.set_rotation` 渲染语义对齐，单测钉死）：
- angle=90：把原页**顺时针**转 90° 得到 OCR 输入（偶数页的转正方向）；
- angle=270：把原页**逆时针**转 90°（奇数页的转正方向）；
- PIL 的 ROTATE_* 是逆时针计数，故 90° 顺时针 = ``Image.Transpose.ROTATE_270``。

归一化框映射（upright→page，bbox 为 (x, y, w, h)，y 轴向下、origin 左上）：
- angle=90（顺时针转正）：x_p = y_u；y_p = 1 − x_u − w_u；宽高互换；
- angle=270（逆时针转正）：x_p = 1 − y_u − h_u；y_p = x_u；宽高互换。
round-trip 单测保证 forward∘inverse = identity。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from PIL import Image

# 评分参数用 10 组真卷夹具校准（横躺 0.703–0.733 / 转正 0.0 / 错向居中 /
# 近空白无信号，四类全分离——见 Issue #347 设计评论的夹具表）。
_VERTICAL_SHARE_SUSPECT = 0.4      # 竖排行块占比超过此值 → 旋转签名
_VERTICAL_SHARE_FINE = 0.25        # clearly_fine 的上限
_BLOB_AREA_SUSPECT = 0.5           # 单块覆盖页面比例超过此值 → 巨型 blob 签名
# 门⑥ Important：blob 佐证的竖行占比下限——blob>0.5 且 vsh 落在此值以下不算
# 旋转签名（直立 dense-table 页的 blob 解析无竖行，vsh≈0；躺倒页文本行竖排
# vsh≈0.7）。
_BLOB_AREA_CORROBORATE_MIN = 0.2
_MIN_CJK_FINE = 30                 # CJK 存活数下限（与长数字游程二选一）
_MIN_LONG_RUNS_FINE = 5            # ≥6 位数字游程数下限
_VERTICAL_SHARE_WEIGHT = 150.0     # 竖排占比的评分惩罚
_LONG_RUN_WEIGHT = 2.0             # 长数字游程权重（账号/日期是打码主目标）

# Issue#347 关键词清醒判别：对账单类表单页的真 upright 帧必然读出成串表单
# 词（真卷实证 kw=46），横躺/倒置帧读不出（kw=0），且对 VL 输出波动免疫。
FORM_KEYWORDS = (
    "账号", "卡号", "户名", "证件", "查询", "余额", "金额", "摘要", "序号",
    "交易日期", "交易时间", "交易类型", "交易地点", "打印", "币种",
    "Account", "Date", "Amount", "Query",
)
KEYWORD_HITS_MIN = 3               # 候选方向「清醒」的命中下限（46 vs 0 天然空隙）

# 面积在此窗口内的块才参与竖排统计：过小的（标点/水印碎片）与过大的（整页
# blob）都不是「文本行」。
_VERTICAL_AREA_MIN = 0.001
_VERTICAL_AREA_MAX = 0.10

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LONG_DIGIT_RUN_RE = re.compile(r"\d{6,}")


@dataclass
class OrientationScore:
    """一次 OCR 块集的方向质量评分。"""

    angle: int                       # 该块集对应的输入旋转角（0=原样）
    score: float                     # 比较制分数，越高越好（清醒候选间排序用）
    clearly_fine: bool               # 无需重试即可收
    rotation_suspect: bool           # 出现旋转签名 → 值得试转正
    keyword_hits: int = 0            # 表单关键词命中数（清醒判别，对 VL 波动免疫）
    vertical_share: float = 0.0      # 竖排行块占比（>0.4 = 文本行竖躺，须归一）
    signals: dict = field(default_factory=dict)


def score_ocr_blocks(blocks: list, width: int, height: int) -> OrientationScore:
    """对 OCR 块集打方向质量分。

    指标：
    - ``vertical_share``：竖排行块（高 > 宽×1.8、面积在文本行窗口内）占比；
      横躺页的文本行在渲染图里是竖排的，横躺输入下此值 ≈0.7。
    - ``blob_area``：最大单块外接框面积占页面比例；VL 把整页并成一个巨型
      表格块时此值逼近 1。
    - ``cjk`` / ``long_digit_runs``：内容存活量（CJK 字符数、≥6 位数字游程数）；
      乱码页 CJK 掉底，转正后显著回升。
    - ``keyword_hits``：表单关键词命中数（``FORM_KEYWORDS``，ASCII 词大小写
      不敏感）——决定性判别器，对 VL 输出波动免疫（真 upright 46 vs 乱码 0）。
    """
    page_area = max(width * height, 1)
    text_blocks = [b for b in blocks if str(getattr(b, "text", "") or "").strip()]
    n = len(text_blocks)

    cjk = 0
    long_runs = 0
    keyword_hits = 0
    blob_area = 0.0
    vertical = 0
    for b in text_blocks:
        text = str(getattr(b, "text", "") or "")
        cjk += len(_CJK_RE.findall(text))
        long_runs += len(_LONG_DIGIT_RUN_RE.findall(text))
        lowered = text.lower()
        keyword_hits += sum(lowered.count(kw.lower()) for kw in FORM_KEYWORDS)
        xs = [p[0] for p in (getattr(b, "polygon", None) or [])]
        ys = [p[1] for p in (getattr(b, "polygon", None) or [])]
        if not xs or not ys:
            continue
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        area = max(w * h, 1)
        blob_area = max(blob_area, area / page_area)
        if (_VERTICAL_AREA_MIN * page_area < area < _VERTICAL_AREA_MAX * page_area
                and h > 1.8 * w):
            vertical += 1

    vertical_share = vertical / n if n else 0.0
    score = float(cjk) + _LONG_RUN_WEIGHT * long_runs - _VERTICAL_SHARE_WEIGHT * vertical_share
    clearly_fine = (
        vertical_share <= _VERTICAL_SHARE_FINE
        and blob_area <= _BLOB_AREA_SUSPECT
        and (cjk >= _MIN_CJK_FINE or long_runs >= _MIN_LONG_RUNS_FINE)
    )
    # 门⑥ Important：巨型 blob 单独不构成旋转证据（直立 dense-table 合同/判决页
    # 也会并成单块，vsh≈0）⇒ 必须同时有竖行佐证（vsh>0.2）才算旋转签名，
    # 否则直立 blob 页会白跑 2 次 OCR 再 fail-closed。躺倒页真并入单块时块本身
    # 水平（vsh=0）⇒ 不触发，保持原样（blob 解析），不产生误失败。
    rotation_suspect = vertical_share > _VERTICAL_SHARE_SUSPECT or (
        blob_area > _BLOB_AREA_SUSPECT and vertical_share > _BLOB_AREA_CORROBORATE_MIN
    )
    return OrientationScore(
        angle=0,
        score=score,
        clearly_fine=clearly_fine,
        rotation_suspect=rotation_suspect,
        keyword_hits=keyword_hits,
        vertical_share=vertical_share,
        signals={
            "blocks": n,
            "cjk": cjk,
            "long_digit_runs": long_runs,
            "keyword_hits": keyword_hits,
            "vertical_share": round(vertical_share, 3),
            "blob_area": round(blob_area, 3),
        },
    )


class OrientationUnresolvedError(RuntimeError):
    """Issue#347：旋转嫌疑页的所有候选方向都读不出表单文本（关键词不清醒）。

    专类参照 ``RoutingEvidenceTimeout`` 先例：``vision_service.
    is_page_fatal_stage_error`` 把本类升级为页级失败（#199 语义——
    ``recognition.failed_pages`` 记账、交付闸 409），而非「stage failed + 页
    completed」的静默放行。产生形态：as-is 竖排/巨型 blob 签名 + 候选方向 OCR
    失败或全部乱码（关键词全灭）——此时比较制分数已被证明不可信（批量 VL
    波动可让倒置乱码帧反超真转正帧），任选一向都是赌博。
    """


def choose_orientation(as_is: OrientationScore, candidates: dict[int, dict]) -> dict:
    """纯决策：as-is 评分 + 各候选 ``{angle: {"score", "keyword_hits"}}``。

    返回 ``{"angle", "retried", "resolved", "scores"}``。规则（关键词清醒
    判别取代比较制分数择优，Issue#347 复跑实证）：

    - as-is 无旋转签名 → 0（好页零开销，绝不重试）；
    - as-is 清醒（kw ≥ ``KEYWORD_HITS_MIN``）**且文本行水平**
      （``vertical_share`` ≤ 0.4）→ 0：原样即正（真 upright 或仅 blob 解析
      粗糙），再旋转只会引入「把可读页转歪」的新风险；
    - **as-is 竖躺（vertical_share > 0.4）⇒ 一律归一，kw 不构成保持理由**：
      竖躺页的 as-is OCR 也可能读出成串表单词（VL 侧读文本并不罕见，复跑
      实证奇数页 as-is kw=43–45），但竖排几何仍会截断字符框——复跑实证
      p9（保持原样）证号框 h=0.053 只盖 ~11/18 位、p10（归一）h=0.096 全盖；
    - 存在清醒候选 → 取分数最高的清醒候选。**不再要求跨 as-is 分数边际**
      ——真转正候选的批内评分可被 VL 波动压低到垃圾 as-is 之下（复跑实证
      p10：90° 真候选 227 < as-is 241×1.2），边际判据会把可救页推向误回退；
      分数仅在多个清醒候选间排序（实际近不存在——另一向必为倒置乱码）；
    - 无清醒候选且 as-is 旋转嫌疑 → ``resolved=False``：调用方按 #199 语义
      升级页级显式失败（宁可显式失败，不可静默少打码）。
    """
    scores = {"0": as_is.score}
    if not as_is.rotation_suspect:
        return {"angle": 0, "retried": False, "resolved": True, "scores": scores}
    scores.update({str(a): float(d["score"]) for a, d in candidates.items()})
    # 「文本行水平」的边界用 _VERTICAL_SHARE_SUSPECT（0.4，= 旋转签名门限）而非
    # clearly_fine 的 _VERTICAL_SHARE_FINE（0.25）：as-is vsh 落在 0.25–0.4 时
    # 文本行总体仍水平（少量竖块混入），保持原样的风险可接受；勿改成 0.25。
    if (
        as_is.keyword_hits >= KEYWORD_HITS_MIN
        and as_is.vertical_share <= _VERTICAL_SHARE_SUSPECT
    ):
        return {"angle": 0, "retried": True, "resolved": True, "scores": scores}
    sober = {
        a: d for a, d in candidates.items()
        if int(d.get("keyword_hits", 0) or 0) >= KEYWORD_HITS_MIN
    }
    if sober:
        best = max(sober, key=lambda a: float(sober[a]["score"]))
        return {"angle": int(best), "retried": True, "resolved": True, "scores": scores}
    return {"angle": 0, "retried": True, "resolved": False, "scores": scores}


# ---------------------------------------------------------------- 图像旋转

def rotate_image_for_ocr(image: Image.Image, angle: int) -> Image.Image:
    """把页图转 ``angle`` 角得到 OCR 输入（与 PyMuPDF set_rotation 渲染语义一致）。

    angle=90 → 顺时针 90°（PIL ROTATE_270）；angle=270 → 逆时针 90°（PIL ROTATE_90）。
    """
    if angle == 90:
        return image.transpose(Image.Transpose.ROTATE_270)
    if angle == 270:
        return image.transpose(Image.Transpose.ROTATE_90)
    if angle == 0:
        return image
    raise ValueError(f"unsupported orientation angle: {angle}")


# ---------------------------------------------------------------- 归一化框映射

def map_normalized_box_to_page(
    box: tuple[float, float, float, float], angle: int
) -> tuple[float, float, float, float]:
    """转正空间 (x, y, w, h) → 原页空间。angle 为产生 OCR 输入所转的角度。"""
    x, y, w, h = box
    if angle == 90:
        # 原页顺时针转 90° 得到转正图：竖排窄高框 ↔ 横排宽扁框互换
        return (y, 1.0 - x - w, h, w)
    if angle == 270:
        return (1.0 - y - h, x, h, w)
    if angle == 0:
        return (x, y, w, h)
    raise ValueError(f"unsupported orientation angle: {angle}")


def map_normalized_box_to_upright(
    box: tuple[float, float, float, float], angle: int
) -> tuple[float, float, float, float]:
    """原页空间 → 转正空间（``map_normalized_box_to_page`` 的逆，round-trip 钉死）。"""
    x, y, w, h = box
    if angle == 90:
        return (1.0 - y - h, x, h, w)
    if angle == 270:
        return (y, 1.0 - x - w, h, w)
    if angle == 0:
        return (x, y, w, h)
    raise ValueError(f"unsupported orientation angle: {angle}")


def map_bounding_box_to_page(box, angle: int):
    """把归一化 ``BoundingBox`` 从转正空间映射回原页空间（保留其余字段）。

    用于 ``_detect_with_ocr_has`` 出口的单点逆映射——下游（file_store 落库、
    马赛克渲染、预览、人工框合并）全部只见原页坐标，零改动。
    """
    if angle == 0:
        return box
    x, y, w, h = map_normalized_box_to_page(
        (float(box.x), float(box.y), float(box.width), float(box.height)), angle
    )
    return box.model_copy(update={"x": x, "y": y, "width": w, "height": h})


def map_polygon_to_page(polygon, angle: int, page_width: float, page_height: float):
    """转正空间（OCR 输入图）像素坐标多边形 → 原页像素坐标（点级逆映射）。

    与 ``map_normalized_box_to_page`` 同一变换的点级形态，供把 winner 空间的
    ``OCRTextBlock``（``polygon``/``chars`` 均为输入图像素坐标）整体映射回原页
    空间——下游 ``_page_ocr_blocks`` 消费者（幻觉卡闸/签名检测器）按原页坐标
    解释它们。``page_width``/``page_height`` 为原页像素尺寸（输入图宽高互换）。
    """
    if angle == 0:
        return polygon
    # 两个角度的输入图宽高互换相同（= 原页高 × 原页宽）；只有点公式分向
    iw, ih = float(page_height), float(page_width)
    pw, ph = float(page_width), float(page_height)
    out = []
    for point in polygon:
        x, y = float(point[0]), float(point[1])
        xn, yn = x / iw, y / ih
        if angle == 90:
            xp, yp = yn, 1.0 - xn
        elif angle == 270:
            xp, yp = 1.0 - yn, xn
        elif angle == 0:
            xp, yp = xn, yn
        else:
            raise ValueError(f"unsupported orientation angle: {angle}")
        out.append([xp * pw, yp * ph])
    return out
