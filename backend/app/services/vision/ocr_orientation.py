"""Issue#347：扫描页 ±90° 方向归一（try-and-pick）——纯函数层。

真卷实证（6号案卷对账单，Issue #347）：双面扫描奇偶页交替 ±90° 躺倒、PDF 无
/Rotate 标志，OCR 服务端方向分类全关，横躺输入让 PaddleOCR-VL 退化（0 框 /
巨型表格 blob / 乱码），表头字段读不出 → 打码整行漏打；竖排文本行在「横排行」
假设的字符框逻辑下被截断 → 证件号只遮前段。

修法：OCR 前不假设方向——as-is 块评分，出现「旋转签名」（竖排行块占比高或
巨型 blob）时把页图转 90°/270° 各补一次纯 OCR，比较制择优；胜者空间做 NER/
匹配，归一化框在 `_detect_with_ocr_has` 出口单点映射回原页坐标。好页零额外
开销（不触发重试），坏页不浪费 NER（重试只跑 OCR）。

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
_MIN_CJK_FINE = 30                 # CJK 存活数下限（与长数字游程二选一）
_MIN_LONG_RUNS_FINE = 5            # ≥6 位数字游程数下限
_VERTICAL_SHARE_WEIGHT = 150.0     # 竖排占比的评分惩罚
_LONG_RUN_WEIGHT = 2.0             # 长数字游程权重（账号/日期是打码主目标）
WINNER_MARGIN_RATIO = 1.2          # 胜者必须显著优于 as-is，否则保持原样

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
    score: float                     # 比较制分数，越高越好
    clearly_fine: bool               # 无需重试即可收
    rotation_suspect: bool           # 出现旋转签名 → 值得试转正
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
    """
    page_area = max(width * height, 1)
    text_blocks = [b for b in blocks if str(getattr(b, "text", "") or "").strip()]
    n = len(text_blocks)

    cjk = 0
    long_runs = 0
    blob_area = 0.0
    vertical = 0
    for b in text_blocks:
        text = str(getattr(b, "text", "") or "")
        cjk += len(_CJK_RE.findall(text))
        long_runs += len(_LONG_DIGIT_RUN_RE.findall(text))
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
    rotation_suspect = vertical_share > _VERTICAL_SHARE_SUSPECT or blob_area > _BLOB_AREA_SUSPECT
    return OrientationScore(
        angle=0,
        score=score,
        clearly_fine=clearly_fine,
        rotation_suspect=rotation_suspect,
        signals={
            "blocks": n,
            "cjk": cjk,
            "long_digit_runs": long_runs,
            "vertical_share": round(vertical_share, 3),
            "blob_area": round(blob_area, 3),
        },
    )


def choose_orientation(as_is: OrientationScore, candidate_scores: dict[int, float]) -> dict:
    """纯决策：给定 as-is 评分与各候选角的分数，返回 {"angle", "retried", "scores"}。

    规则：
    - as-is 无旋转签名 → 0（好页零开销，绝不重试）；
    - 有签名 → 取分数最高的候选角；但胜者必须比 as-is 高出
      ``WINNER_MARGIN_RATIO`` 倍以上，否则保持 0（宁可维持现状，不引入
      「把正常页转歪」的新风险）。
    """
    if not as_is.rotation_suspect:
        return {"angle": 0, "retried": False, "scores": {"0": as_is.score}}
    best_angle, best_score = 0, as_is.score
    for angle, s in candidate_scores.items():
        if s > best_score:
            best_angle, best_score = int(angle), float(s)
    if best_angle != 0 and best_score < as_is.score * WINNER_MARGIN_RATIO:
        best_angle = 0
    return {
        "angle": best_angle,
        "retried": True,
        "scores": {"0": as_is.score, **{str(k): v for k, v in candidate_scores.items()}},
    }


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
