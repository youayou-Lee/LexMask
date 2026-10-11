"""T3 四臂适配器：统一 `run_arm(arm, image_path) -> list[PredBox]` 接口。

坐标契约：返回的 PredBox 一律**页像素、左上原点**（spec.py docstring）。
各臂的原始坐标系与换算：

- la            LA /detect（locate_anything_server.py, 端口 8090）。请求
  `{"image_base64":..., "categories":["official_seal","signature"]}`。响应
  `{"boxes":[{x,y,width,height,category,confidence?},...], "raw_answer":...}`。
  raw_answer 按 `[category]` 前缀分段，段内 `<box><x1><y1><x2><y2></box>` 为
  **0-1000 相对坐标**（对齐 locate_anything_eval._parse_boxes），本适配器优先
  解析 raw_answer 的 0-1000 坐标 × 页宽高 → 像素（kind 按通道：
  official_seal→seal、signature→signature）；raw_answer 解析不出时回退响应
  的 0-1 归一化结构化 boxes × 页宽高。confidence：HF 模式无分数 → 按 1.0
  （与 MinerU「无分数填 1.0」口径一致，非测量值）；vLLM logprobs 分数有则透传。

- mineru_seal   MinerU sidecar（16581）。请求/响应形态全部对齐 T0
  probe_mineru.py 实证契约（perf-work/issue92-t0/）：POST /tasks（multipart
  data={"return_content_list":"true","backend":"pipeline","lang_list":"ch"}）
  → 202 {task_id} → 轮询 GET /tasks/{id}（status|state 键；终态
  completed/done/finished，失败 failed/error）→ GET /tasks/{id}/result 取
  zip，优先 *_content_list_v2.json、无则 *_content_list.json。content 是
  list-of-pages（每页=list of blocks）或扁平 list（两种都处理），seal 块
  `{"type":"seal","bbox":[x0,y0,x1,y1]}` → 页像素（云端冒烟实证，T2.1 落定），
  confidence 恒 1.0（块无分数，写死非测量值）。

- paddlex_seal  本地函数式封装 paddlex seal_recognition 产线（env
  T3_PADDLEX_PIPELINE，默认 "seal_recognition"，PaddleX 3.7.2 官方产线名）。
  真实输出 SealRecognitionResult（云端含章页 p11 实测）：
  `res["layout_det_res"]["boxes"]` 里 label=="seal" 的项
  {"label","score","coordinate":[x1,y1,x2,y2]}（页像素，与 LA 框对得上）为
  主路径；layout 无 seal 时回退 `res["seal_res_list"][i]["dt_polys"]`
  （弯曲文本多边形 N×2）外接矩形并集。confidence = layout score（版面模型
  分数，非 seal_det 框置信）。pipeline 实例按名 lazy 单例缓存（冷启动
  35-40s/次）。paddlex 未安装时 raise RuntimeError（明确报错，由跑批层记
  failed）。

- sigdet        sigdet /detect（signature_detector_server.py, 端口 28150）。请求
  `{"image_base64":..., "threshold"?}`；响应 boxes 为 **0-1 归一化**
  {x,y,width,height,confidence} × 页宽高 → 像素，kind 恒 signature。

统一纪律：每臂单页调用带超时与重试（HTTP 层 1 次重试，5xx/超时/连接错误
可重试）；任何异常向上抛（不静默吞），由 benchmark_t3.py 捕获后把该页记
failed —— 失败页 >10% 时该臂判无效（spec.is_arm_invalid）。
"""

from __future__ import annotations

import base64
import io
import os
import re
import time
import zipfile
from typing import Any

import httpx
from PIL import Image

ARMS = ("la", "mineru_seal", "paddlex_seal", "sigdet")

LA_KIND_BY_CHANNEL = {"official_seal": "seal", "signature": "signature"}

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MINERU_POLL_TIMEOUT_S = 600.0
DEFAULT_MINERU_POLL_INTERVAL_S = 1.0

# LA raw_answer 里 0-1000 制 box token（对齐 locate_anything_eval 的正则）
_LA_REF_BOX = re.compile(
    r"<ref>(?P<label>.*?)</ref>\s*<box><(?P<x1>\d+)><(?P<y1>\d+)><(?P<x2>\d+)><(?P<y2>\d+)></box>"
)
_LA_SEGMENT = re.compile(r"\[(?P<cat>official_seal|signature)\]")


class ArmError(RuntimeError):
    """单臂单页调用失败（超时/HTTP 错误/响应不合法/缺库）。"""


# ---- 公共小件 --------------------------------------------------------------


def env_url(name: str, default: str) -> str:
    return os.environ.get(name, default).rstrip("/")


def image_size(image_path: str) -> tuple[int, int]:
    with Image.open(image_path) as im:
        return im.size  # (width, height)


def _encode_image(image_path: str) -> str:
    with open(image_path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _post_with_retry(
    url: str,
    *,
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    retries: int = 1,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    """POST，超时/连接错误/5xx 重试 retries 次（默认 1 次），仍失败抛 ArmError。"""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout, transport=transport) as client:
                resp = client.post(url, json=json_body, data=data, files=files)
            if resp.status_code >= 500:
                # 5xx 视为瞬时过载，可重试
                last_error = ArmError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < retries:
                    continue
                raise last_error
            if resp.status_code >= 400:
                # 4xx 是确定性错误，不重试
                raise ArmError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp
        except ArmError:
            raise
        except httpx.HTTPError as exc:  # 超时/连接错误 → 可重试
            last_error = exc
            if attempt < retries:
                continue
    raise ArmError(f"请求失败（已重试 {retries} 次）: {url}: {last_error!r}")


def _http_get(url: str, timeout: float, transport: httpx.BaseTransport | None) -> httpx.Response:
    with httpx.Client(timeout=timeout, transport=transport) as client:
        return client.get(url)


def convert_1000_box(x1, y1, x2, y2, page_w: int, page_h: int) -> tuple[float, float, float, float]:
    """LA 0-1000 相对坐标 → 页像素 (x, y, w, h)，越界截断、宽高非负。"""
    px1 = min(max(0.0, x1 / 1000 * page_w), float(page_w))
    py1 = min(max(0.0, y1 / 1000 * page_h), float(page_h))
    px2 = min(max(0.0, x2 / 1000 * page_w), float(page_w))
    py2 = min(max(0.0, y2 / 1000 * page_h), float(page_h))
    return (px1, py1, max(0.0, px2 - px1), max(0.0, py2 - py1))


def convert_01_box(x, y, w, h, page_w: int, page_h: int) -> tuple[float, float, float, float]:
    """0-1 归一化 (x,y,w,h) → 页像素，越界截断。"""
    px = min(max(0.0, x * page_w), float(page_w))
    py = min(max(0.0, y * page_h), float(page_h))
    pw = min(max(0.0, w * page_w), float(page_w) - px)
    ph = min(max(0.0, h * page_h), float(page_h) - py)
    return (px, py, pw, ph)


def _pred(kind: str, px: tuple[float, float, float, float], confidence: float, raw: str) -> dict:
    return {
        "kind": kind,
        "box": {"x": round(px[0], 2), "y": round(px[1], 2), "w": round(px[2], 2), "h": round(px[3], 2)},
        "confidence": float(confidence),
        "raw": raw,
    }


def parse_la_raw_answer(raw_answer: str, page_w: int, page_h: int) -> list[dict]:
    """解析 LA raw_answer：按 [category] 分段，段内 0-1000 box → 像素。

    段外的裸 <box> token 无通道归属，跳过（kind 不明不能进契约）。
    """
    preds: list[dict] = []
    segments = list(_LA_SEGMENT.finditer(raw_answer))
    for seg_idx, seg in enumerate(segments):
        seg_end = segments[seg_idx + 1].start() if seg_idx + 1 < len(segments) else len(raw_answer)
        body = raw_answer[seg.end():seg_end]
        kind = LA_KIND_BY_CHANNEL.get(seg.group("cat"))
        if kind is None:
            continue
        for m in _LA_REF_BOX.finditer(body):
            px = convert_1000_box(
                int(m.group("x1")), int(m.group("y1")), int(m.group("x2")), int(m.group("y2")),
                page_w, page_h,
            )
            preds.append(_pred(kind, px, 1.0, m.group(0)))
    return preds


# ---- 臂 1：LA --------------------------------------------------------------


def run_la(
    image_path: str,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    url = (base_url or env_url("T3_LA_URL", "http://127.0.0.1:8090")) + "/detect"
    page_w, page_h = image_size(image_path)
    resp = _post_with_retry(
        url,
        json_body={
            "image_base64": _encode_image(image_path),
            "categories": ["official_seal", "signature"],
        },
        timeout=timeout,
        transport=transport,
    )
    payload = resp.json()
    preds = parse_la_raw_answer(str(payload.get("raw_answer") or ""), page_w, page_h)
    if preds:
        return preds
    # 回退：raw_answer 没解析出（服务端格式变更）→ 用结构化 0-1 boxes
    for b in payload.get("boxes") or []:
        kind = LA_KIND_BY_CHANNEL.get(str(b.get("category") or ""))
        if kind is None:
            continue
        px = convert_01_box(b["x"], b["y"], b["width"], b["height"], page_w, page_h)
        preds.append(_pred(kind, px, float(b.get("confidence") or 1.0), ""))
    return preds


# ---- 臂 2：MinerU seal -----------------------------------------------------


def _mineru_poll_once(
    base_url: str, task_id: str, timeout: float, transport: httpx.BaseTransport | None
) -> str:
    """终态归一：响应 status 或 state 键（T0 probe_mineru.py 实证两者皆可能）；
    终态字面值 completed/done/finished，failed/error 为失败。"""
    try:
        resp = _http_get(f"{base_url}/tasks/{task_id}", timeout, transport)
    except httpx.HTTPError as exc:
        raise ArmError(f"mineru poll 失败: {exc!r}") from exc
    if resp.status_code >= 400:
        raise ArmError(f"mineru poll HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    return str(payload.get("status") or payload.get("state") or "")


_MINERU_DONE = {"completed", "done", "finished"}
_MINERU_FAILED = {"failed", "error"}


def _extract_seal_preds_from_content_list(content: Any, page_w: int, page_h: int) -> list[dict]:
    """content_list → seal 预测（真实块形态，T0 冒烟实证，勿再猜）。

    JSON 两种形态都处理：list-of-pages（每页=list of blocks，T0 实证）或扁平
    list。block 形如 ``{"type": "seal", "bbox": [x0, y0, x1, y1], ...}``——
    type=="seal" 直取（冒烟实证页像素，T2.1 落定）；兼容旧 sub_type=="seal"。
    confidence 恒 1.0（块无分数）。
    """
    preds: list[dict] = []
    if not isinstance(content, list):
        return preds
    page_lists: list[Any] = (
        content if (content and isinstance(content[0], list)) else [content]
    )
    for blocks in page_lists:
        for item in blocks or []:
            if not isinstance(item, dict):
                continue
            btype = str(item.get("type") or "")
            is_seal = btype == "seal" or (
                btype == "image" and item.get("sub_type") == "seal"
            )
            if not is_seal:
                continue
            bbox = item.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = (float(v) for v in bbox)
            w, h = max(0.0, x1 - x0), max(0.0, y1 - y0)
            if w <= 0 or h <= 0:
                continue
            preds.append(_pred("seal", (x0, y0, w, h), 1.0, f"type={btype}"))
    return preds


def run_mineru_seal(
    image_path: str,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    poll_timeout: float = DEFAULT_MINERU_POLL_TIMEOUT_S,
    poll_interval: float = DEFAULT_MINERU_POLL_INTERVAL_S,
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    """提交单页图像给 MinerU sidecar，轮询到终态后从 zip content_list 取 seal 块。

    请求/响应形态全部对齐 T0 probe_mineru.py 实证契约（勿改回猜的键）：
    POST /tasks multipart data={"return_content_list":"true","backend":"pipeline",
    "lang_list":"ch"}（不带 zip 开关，result 端点本来就回 zip）→ 202 {task_id}；
    GET /tasks/{id} 看 status|state；GET /tasks/{id}/result 取 zip，优先
    *_content_list_v2.json、无则 *_content_list.json。
    """
    url = base_url or env_url("T3_MINERU_URL", "http://127.0.0.1:16581")
    page_w, page_h = image_size(image_path)
    with open(image_path, "rb") as fh:
        image_bytes = fh.read()
    resp = _post_with_retry(
        f"{url}/tasks",
        data={
            "return_content_list": "true",
            "backend": "pipeline",
            "lang_list": "ch",
        },
        files={"files": (os.path.basename(image_path), image_bytes, "image/png")},
        timeout=timeout,
        transport=transport,
    )
    try:
        task_id = resp.json()["task_id"]
    except (ValueError, KeyError) as exc:
        raise ArmError(f"mineru submit 响应缺 task_id: {resp.text[:200]}") from exc

    deadline = time.monotonic() + poll_timeout
    while True:
        status = _mineru_poll_once(url, task_id, timeout, transport)
        if status in _MINERU_DONE:
            break
        if status in _MINERU_FAILED:
            raise ArmError(f"mineru task failed: {task_id}")
        if time.monotonic() > deadline:
            raise ArmError(f"mineru task 轮询超时: {task_id}")
        time.sleep(poll_interval)

    try:
        result = _http_get(f"{url}/tasks/{task_id}/result", timeout, transport)
    except httpx.HTTPError as exc:
        raise ArmError(f"mineru result 拉取失败: {exc!r}") from exc
    if result.status_code >= 400:
        raise ArmError(f"mineru result HTTP {result.status_code}")
    with zipfile.ZipFile(io.BytesIO(result.content)) as zf:
        names = [n for n in zf.namelist() if n.endswith("_content_list_v2.json")]
        if not names:
            names = [n for n in zf.namelist() if n.endswith("_content_list.json")]
        if not names:
            raise ArmError("mineru zip 内无 *_content_list(_v2).json")
        preds: list[dict] = []
        for name in names:
            content = json_loads(zf.read(name))
            preds.extend(_extract_seal_preds_from_content_list(content, page_w, page_h))
    return preds


def json_loads(data: bytes) -> Any:
    import json

    return json.loads(data.decode("utf-8"))


# ---- 臂 3：PaddleX seal_recognition ----------------------------------------

# pipeline 实例按名缓存（lazy 单例）：create_pipeline 冷启动 35-40s/次
# （T4 实测，模型加载占大头），逐页新建整批要白烧几十分钟。
_PIPELINE_CACHE: dict[str, Any] = {}


def _get_paddlex_pipeline(name: str):
    from paddlex import create_pipeline

    if name not in _PIPELINE_CACHE:
        _PIPELINE_CACHE[name] = create_pipeline(name)
    return _PIPELINE_CACHE[name]


def _parse_paddlex_seal_output(outputs: Any) -> list[dict]:
    """解析 SealRecognitionResult → 统一契约（真实结构，云端含章页 p11 实测，勿再猜）。

    主路径：``res["layout_det_res"]["boxes"]`` 里 label=="seal" 的项
    ``{"label":"seal","score":0.96,"coordinate":[x1,y1,x2,y2]}``（页像素，
    与 LA 框对得上）。
    回退：layout 无 seal 时取 ``res["seal_res_list"][i]["dt_polys"]``（弯曲
    文本多边形 N×2）的外接矩形并集。
    兼容：旧 ``res["boxes"]`` 的 {x,y,w,h,confidence} / coordinate 形态。
    confidence = layout score（版面模型分数，非 seal_det 框置信）。
    """
    preds: list[dict] = []
    for res in outputs or []:
        if not isinstance(res, dict):
            continue
        # 主路径：layout_det_res.boxes 里 label=="seal"
        for box_item in (res.get("layout_det_res") or {}).get("boxes") or []:
            if str(box_item.get("label") or "") != "seal":
                continue
            coord = box_item["coordinate"]
            x1, y1 = float(coord[0]), float(coord[1])
            x2, y2 = float(coord[2]), float(coord[3])
            w, h = max(0.0, x2 - x1), max(0.0, y2 - y1)
            if w <= 0 or h <= 0:
                continue
            score = box_item.get("score")
            preds.append(_pred("seal", (x1, y1, w, h), 1.0 if score is None else float(score), "layout:seal"))
        if preds:
            continue
        # 回退：seal_res_list 各项 dt_polys 外接矩形并集
        for seal_item in res.get("seal_res_list") or []:
            polys = seal_item.get("dt_polys") or []
            # dt_polys 形态兼容：单个 N×2 多边形 [[x,y],...]，或多边形列表 [[[x,y],...],...]
            points: list[tuple[float, float]] = []

            def _add_point(q) -> None:
                points.append((float(q[0]), float(q[1])))

            for p in polys:
                if not p:
                    continue
                if isinstance(p[0], (list, tuple)):
                    if p[0] and isinstance(p[0][0], (list, tuple)):
                        for q in p:  # 多边形列表：每个 q 是 N×2 多边形
                            for r in q:
                                _add_point(r)
                    else:  # p 本身是 N×2 多边形
                        for q in p:
                            _add_point(q)
                else:
                    _add_point(p)
            if not points:
                continue
            xs = [pt[0] for pt in points]
            ys = [pt[1] for pt in points]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            w, h = max(0.0, x2 - x1), max(0.0, y2 - y1)
            if w <= 0 or h <= 0:
                continue
            score = seal_item.get("score")
            preds.append(_pred("seal", (x1, y1, w, h), 1.0 if score is None else float(score), "dt_polys"))
        if preds:
            continue
        # 兼容：旧 res["boxes"] 形态（{x,y,w,h,confidence} 或 coordinate 四元组）
        for box_item in res.get("boxes") or []:
            if "coordinate" in box_item:  # 原生 layout 结果形态
                coord = box_item["coordinate"]
                x1, y1 = float(coord[0]), float(coord[1])
                x2, y2 = float(coord[2]), float(coord[3])
                w, h = max(0.0, x2 - x1), max(0.0, y2 - y1)
                score = box_item.get("score")
            else:  # T4 CLI 实测形态：x/y/w/h 已是页像素
                x1, y1 = float(box_item["x"]), float(box_item["y"])
                w, h = float(box_item["w"]), float(box_item["h"])
                score = box_item.get("confidence")
            if w <= 0 or h <= 0:
                continue
            preds.append(_pred("seal", (x1, y1, w, h), 1.0 if score is None else float(score), ""))
    return preds


def run_paddlex_seal(image_path: str, pipeline_name: str | None = None) -> list[dict]:
    """本地函数式调用 paddlex seal_recognition 产线（T4 实测，勿改回裸模型直调——
    PP-OCRv4_server_seal_det 全页直跑假阳性暴涨，必须经版面 seal 区域裁剪）。

    paddlex 缺失 → 明确 RuntimeError；pipeline 实例按名缓存（冷启动 35-40s，
    见 _PIPELINE_CACHE）。confidence = 版面模型分数（语义见 _parse_paddlex_seal_output）。
    """
    name = pipeline_name or os.environ.get("T3_PADDLEX_PIPELINE", "seal_recognition")
    try:
        pipeline = _get_paddlex_pipeline(name)
    except ImportError as exc:
        raise RuntimeError(
            "paddlex 未安装（pip install paddlex 后重试，或该臂按预期标 failed）"
        ) from exc
    outputs = pipeline.predict(image_path)
    return _parse_paddlex_seal_output(outputs)


# ---- 臂 4：sigdet ----------------------------------------------------------


def run_sigdet(
    image_path: str,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    url = (base_url or env_url("T3_SIGDET_URL", "http://127.0.0.1:28150")) + "/detect"
    page_w, page_h = image_size(image_path)
    resp = _post_with_retry(
        url, json_body={"image_base64": _encode_image(image_path)}, timeout=timeout, transport=transport
    )
    payload = resp.json()
    preds: list[dict] = []
    for b in payload.get("boxes") or []:
        px = convert_01_box(b["x"], b["y"], b["width"], b["height"], page_w, page_h)
        preds.append(_pred("signature", px, float(b.get("confidence") or 0.0), f"mode={payload.get('mode')}"))
    return preds


# ---- 统一入口 --------------------------------------------------------------


_ARM_RUNNERS = {
    "la": "run_la",
    "mineru_seal": "run_mineru_seal",
    "paddlex_seal": "run_paddlex_seal",
    "sigdet": "run_sigdet",
}


def run_arm(arm: str, image_path: str) -> list[dict]:
    """四臂统一入口。失败抛异常（ArmError/RuntimeError），由上层记 failed。"""
    func_name = _ARM_RUNNERS.get(arm)
    if func_name is None:
        raise ArmError(f"未知臂: {arm!r}（允许 {ARMS}）")
    # globals() 动态取，测试 monkeypatch 才生效
    return globals()[func_name](image_path)
