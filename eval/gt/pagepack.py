"""逐页预标流水线：转录 → 归一化 → 比对 → 实体 → 仲裁 → pagepack（Issue#56 Task 7）。

GT 管线的装配层：把 Task 1-6 的模块按 spec §3 数据流串成单页 run，产出
T6 契约形状的 pagepack 并落盘。本模块只做装配与 gate，不做参数解析
（CLI 在 run_pipeline.py）。

单页数据流（``run_page``）::

    双云转录（+可选 vl-md）→ 各面 FaceMap（Task 2）
      → compare_transcripts（Task 4，归一化面）
      → 三通道实体（Task 3：regex+NER 于 v6 面、VL 面各跑，vl-md 面按需）
      → arbitrate_page（Task 5，R1-R7）
      → 组装 pack（spec §4 形状）→ 自校验（T6 validate_pagepack，错误拒写）
      → 写 {work_dir}/pages/{page_id}/pack.json → 返回 pack

关键裁定（实现遵循，详见 task-7-report）：

- **采信面**（v2 修订）：独有采信读数（R3/R4 整面采信、R2-rev/R6-rev 单方
  采信）落在哪面就采哪面——键只在 b 面（R3 采 VL / 单方采 b）→ 采 b 面，
  否则一律采 a（v6）面；面无关条目（R1/R2 正则胜/R7 同读组，键两面皆在）
  不构成面要求。R5/整页升级页同样默认采 a 侧——fidelity 恒为 "machine"，
  定稿由工作台回填（spec §4：human-reviewed 由人工阶段写入）。
- **实体面归属**：实体 span 是双面的、以各自转录面为坐标（T3/T5 裁定）。
  pack 的 transcript_gt 是单一采信面，故 pack 实体一律以**采信面上的实例**
  为 span 基准：一致桶/采信桶读数按键（span_normalized, text）在采信面
  定位（一致读数两云同键、R3/R4 采信读数本就在采信面，定位必然命中，
  未命中视为管线不变量破坏、fail-fast）；R2 类型冲突的胜出类型以仲裁
  结论为准覆盖实例类型。未决（disputed）读数不进 entities——VERIFY_STATES
  无「disputed」态，其去留由工作台裁决后回填；读数本体连同三方候选完整
  留痕在 adjudications.candidates（A4 无静默丢弃）。
- **仲裁输出只读**：T5 输出实体为浅拷贝共享，本层只读消费，pack 结构全部
  新建（浅拷贝组装，不原地改写）。
- **adjudications**：一致桶逐条 ``{"models", "rule": "R1", "verdict":
  "consistent"}``；仲裁桶逐条 ``{"models", "rule", "verdict": "auto:<source>"}``；
  争议桶逐条 ``{"models", "rule", "verdict": "disputed", "candidates"}``
  （页级 gap 条目额外带 ``gap`` 文案）。models = 实际参与的通道
  ``["v6", "vl"][+ "vl-md"]``。candidates 内的实体保持其各自面的坐标
  （不换算到采信面——换算即失真，工作台按面渲染）。
- **file_sha256**：对原件实算（页 ID 回溯锚点，spec §4 / D 闸）；文件缺失
  fail-fast，宁可不产 pack 不可造伪锚点。
- **NER 类型名校验**（T3 deferred Minor#4 的 T7 决定）：不在管线层预过滤
  NER 返回的类型名——preset 之外的类型会卡在 pack 自校验闸上显式失败，
  静默过滤反而会掩盖 backend 端点漂移。

离线约定：本模块自身零网络（转录/NER 均经注入的客户端）；测试一律注入
fake 客户端，绝不实例化真实云客户端发起请求。
"""
from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

import requests

from gt.arbitrate import arbitrate_page
from gt.compare import compare_transcripts
from gt.entities import Entity, NERClient  # noqa: F401（Entity/NERClient 为类型再导出）
from gt.entities import extract_ner, extract_regex, merge_entities
from gt.gt_schema import PAGE_TYPES, PRESET_TYPE_NAMES, validate_pagepack
from gt.normalize import FaceMap
from gt.unify import TranscriptionClient  # noqa: F401（协议再导出）

_REPO_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)

# 每页 pack 的落盘子目录布局：{work_dir}/pages/{page_id}/pack.json
_PACK_RELPATH = Path("pages")


# ---- 客户端侧附件 ----------------------------------------------------------------

_NERQ_CACHE: dict = {}


def _load_ner_quality():
    """按路径加载 backend/scripts/eval/eval_ner_quality.py（P/R 与提示词唯一口径）。"""
    if not _NERQ_CACHE:
        import importlib.util
        path = _REPO_ROOT / "backend" / "scripts" / "eval" / "eval_ner_quality.py"
        spec = importlib.util.spec_from_file_location("eval_ner_quality", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _NERQ_CACHE["m"] = mod
    return _NERQ_CACHE["m"]


class OpenAINERClient:
    """vLLM/OpenAI 兼容 NER 客户端（NERClient 协议）——**实测真实形状**。

    对齐 ``backend/scripts/eval/eval_ner_quality.py::call_ner``：POST
    ``{base}/chat/completions``，体 = build_ner_prompt(text, preset 类型全集)
    + temperature 0.0 / top_p 0.6 / max_tokens；解析用 parse_model_json
    （围栏剥离+子串提取，与 has_client 同序）。``base`` 以 ``/v1`` 结尾
    （如 ``http://127.0.0.1:8080/v1``），``model`` 可省（单模型 vLLM 忽略）。
    2026-10-06 已对实例 8080（HaS_Text_0209_0.6B）实测。

    硬墙钟（2026-10-07 黑洞连接事件回归）：requests 的 read timeout 对
    半开 TCP / 服务端 stall 不生效（整进程挂 31+ 分钟的实战教训），故
    ``ner()`` 把 POST 放进工作线程，主线程以 deadline 强制等待——到点
    即抛 RuntimeError，不信任传输层超时。
    """

    def __init__(self, base_url: str, model: str | None = None, types: list[str] | None = None,
                 max_tokens: int = 1024, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.types = sorted(types) if types is not None else sorted(PRESET_TYPE_NAMES)
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=1)
        nerq = _load_ner_quality()
        self._build_prompt = nerq.build_ner_prompt
        self._parse = nerq.parse_model_json
        self._temperature = nerq._MODEL_TEMPERATURE
        self._top_p = nerq._MODEL_TOP_P

    def _post(self, text: str) -> dict[str, list[str]]:
        payload = {
            "messages": [{"role": "user", "content": self._build_prompt(text, self.types)}],
            "temperature": self._temperature, "top_p": self._top_p,
            "stream": False, "max_tokens": self.max_tokens,
        }
        if self.model:
            payload["model"] = self.model
        r = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"NER 调用失败 status={r.status_code} body[:300]={r.text[:300]}")
        content = r.json()["choices"][0]["message"]["content"]
        return self._parse(content) or {}

    def ner(self, text: str) -> dict[str, list[str]]:
        future = self._pool.submit(self._post, text)
        try:
            return future.result(timeout=self.timeout)
        except FuturesTimeoutError:
            future.cancel()
            raise RuntimeError(
                f"NER 调用硬墙钟超时（>{self.timeout:g}s）——疑似黑洞连接"
                "（requests 超时未触发；见 2026-10-07 黑洞连接事件）") from None


class HTTPNERClient:
    """backend NER 端点的 HTTP 适配（NERClient 协议）。

    请求：POST ``base_url``，JSON 体 ``{"text": <归一化面文本>,
    "types": [preset 类型名...]}``；响应：``{"entities": {类型名: [实体串, ...]}}``
    （与 backend NER 的「类型中文名 → 实体串列表」协议同形）。

    端点形状待与 backend NER 实测对齐（run_eval 的 ner 层走 OpenAI 兼容
    /chat/completions 提示词协议，本适配假定存在直连 REST 端点）；形状不符
    时此处显式报错，T8/联调阶段第一时间暴露真实形状再校正，不做静默兼容。
    """

    def __init__(self, base_url: str, types: list[str] | None = None,
                 timeout: float = 300.0):
        self.base_url = base_url
        # 类型名全集默认读 preset 单一事实源（gt_schema 动态加载，改名自动跟随）
        self.types = sorted(types) if types is not None else sorted(PRESET_TYPE_NAMES)
        self.timeout = timeout

    def ner(self, text: str) -> dict[str, list[str]]:
        resp = requests.post(self.base_url, json={"text": text, "types": self.types},
                             timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(
                f"NER 端点失败 status={resp.status_code} base={self.base_url} "
                f"body[:200]={resp.text[:200]}")
        body = resp.json()
        entities = body.get("entities") if isinstance(body, dict) else None
        if not isinstance(entities, dict) or not all(
                isinstance(v, list) for v in entities.values()):
            raise RuntimeError(
                "NER 端点响应形状不符（期望 {\"entities\": {类型: [串,...]}}），"
                f"实得 {str(body)[:200]}；端点形状待与 backend NER 实测对齐")
        return entities


class CachedTranscriptionClient:
    """转录缓存代理：同一文件路径只透传一次 ``transcribe``。

    批量模式对同一多页文件逐页调 run_page，若不缓存会每页重打一次云作业；
    本代理按 file_path 记忆首次结果（同一路径返回同一 list 实例），
    run_page 的「len>1 取 pages[page_no]」切片语义不受影响。
    """

    def __init__(self, inner: TranscriptionClient):
        self._inner = inner
        self._cache: dict[str, list] = {}

    def transcribe(self, file_path: str) -> list:
        if file_path not in self._cache:
            self._cache[file_path] = self._inner.transcribe(file_path)
        return self._cache[file_path]


# ---- 单页装配 --------------------------------------------------------------------

def _sha256_hex(path: str) -> str:
    """原件字节的 sha256（流式读取；文件缺失/不可读直接抛错——fail-fast）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_page(pages: list, page_no: int, label: str) -> dict:
    """按页号取转录页：多页结果取 pages[page_no]；单页结果恒取 pages[0]。

    多页 JSONL 的页粒度是 T8 待实测假设（T1），此处只做切片与越界防护。
    """
    if not pages:
        raise ValueError(f"{label} 转录返回空页列表（file_path 不可转录？）")
    if len(pages) > 1:
        if not 0 <= page_no < len(pages):
            raise ValueError(
                f"{label} 转录页数 {len(pages)} 不含第 {page_no} 页（越界）")
        return pages[page_no]
    return pages[0]


def _entity_key(e: Entity) -> tuple:
    """「同读」判键，与 arbitrate 同款：(span_normalized, text)。"""
    return (tuple(e["span_normalized"]), e["text"])


def _locate(face_by_key: dict, key: tuple, prefer_type: str | None) -> Entity:
    """在采信面实体索引中定位读数实例：优先同型，其次首条。"""
    insts = face_by_key.get(key) or []
    if prefer_type is not None:
        for inst in insts:
            if inst["type"] == prefer_type:
                return inst
    if insts:
        return insts[0]
    raise ValueError(
        f"仲裁读数 {key[1]!r}（norm span {list(key[0])}）未在采信面定位到实例"
        "——管线不变量破坏（一致读数应两云同键、采信读数应本在采信面）")


def _pack_entities(arb: dict, ents_face: list[Entity]) -> list[dict]:
    """从仲裁输出装配 pack 实体（全部落在采信面上，见模块 docstring 裁定）。

    - 一致桶 → ``verify="consistent"``、``arbitration="R1"``；
    - 仲裁桶 → ``verify="arbitrated"``、``arbitration=<rule>``，R2 类型冲突
      时以仲裁胜出类型覆盖实例类型；
    - 未决读数不进 entities（无对应 verify 态），由 adjudications 留痕。
    """
    face_by_key: dict[tuple, list[Entity]] = {}
    for e in ents_face:
        face_by_key.setdefault(_entity_key(e), []).append(e)
    out: list[dict] = []
    for e in arb.get("consistent") or []:
        inst = _locate(face_by_key, _entity_key(e), e["type"])
        out.append({**inst, "verify": "consistent", "arbitration": "R1", "note": None})
    for entry in arb.get("auto_resolved") or []:
        win = entry["entity"]
        inst = _locate(face_by_key, _entity_key(win), win["type"])
        out.append({**inst, "type": win["type"], "verify": "arbitrated",
                    "arbitration": entry["rule"], "note": None})
    out.sort(key=lambda e: (e["span_original"][0], e["span_original"][1],
                            e["span_normalized"][0], e["type"], e["text"]))
    return out


def _pack_adjudications(arb: dict, models: list[str]) -> list[dict]:
    """仲裁三桶 → adjudications 留痕（逐条，形状见模块 docstring 裁定）。

    争议条目的 candidates 原样携带各面实例（不换算坐标）；浅拷贝组装，
    不复用仲裁输出容器。
    """
    out: list[dict] = []
    for _ in arb.get("consistent") or []:
        out.append({"models": list(models), "rule": "R1", "verdict": "consistent"})
    for entry in arb.get("auto_resolved") or []:
        out.append({"models": list(models), "rule": entry["rule"],
                    "verdict": f"auto:{entry['source']}"})
    for d in arb.get("disputed") or []:
        candidates = {side: list(insts)
                      for side, insts in (d.get("candidates") or {}).items()}
        item = {"models": list(models), "rule": d["rule"], "verdict": "disputed",
                "candidates": candidates}
        if "gap" in d:
            item["gap"] = d["gap"]  # 页级 gap 条目：gap 文案即 compare 争议证据
        out.append(item)
    return out


def _auto_face_requirements(arb: dict, ents_a: list[Entity],
                            ents_b: list[Entity]) -> set[str]:
    """auto 采信条目的采信面硬要求集合（⊆ {"a","b"}）。

    条目键在另一面**不在场**（R3/R4 独有采信、R2-rev/R6-rev 单方采信）→
    必须落在其 source 面；键两面皆在（R1/R2 正则胜/R7 同读组）→ 面无关、
    不构成要求（沿 v1 默认采 a 面，跨面键由 ``_locate`` 同型优先定位）。
    """
    ka = {_entity_key(e) for e in ents_a or []}
    kb = {_entity_key(e) for e in ents_b or []}
    need: set[str] = set()
    for entry in arb.get("auto_resolved") or []:
        src = entry.get("source")
        if src not in ("a", "b"):
            continue
        key = _entity_key(entry["entity"])
        other = kb if src == "a" else ka
        if key not in other:
            need.add(src)
    return need


def run_page(file_path: str, page_no: int, page_type: str,
             clients: dict[str, TranscriptionClient], ner: NERClient | None,
             work_dir: Path, carrier: str = "scanned", segment: str = "first") -> dict:
    """跑通单页预标流水线，产出并落盘一条 pagepack，返回 pack dict。

    参数：
    - ``file_path``  原件路径（sha256 对其实算，须存在）；
    - ``page_no``    页号（page_id 与 source.page；多页转录按此切片）；
    - ``page_type``  ∈ gt_schema.PAGE_TYPES；
    - ``clients``    ``{"a": 云v6, "b": 云VL, "md": 本地vl-md(可选)}``，
      值为 TranscriptionClient；
    - ``ner``        NERClient 或 None（None/NEROff = 关闭 NER 通道）；
    - ``work_dir``   工作目录，pack 写至 ``{work_dir}/pages/{page_id}/pack.json``；
    - ``carrier`` / ``segment``  原件载体与卷内段位（透传入 source）。

    产出先过 ``validate_pagepack`` 自校验（错误拒写、warning 放行并记日志），
    不合格页绝不落盘（fail-fast，宁缺毋滥）。批量调用方逐页捕获异常即可
    实现失败隔离。
    """
    if page_type not in PAGE_TYPES:
        raise ValueError(f"未知页型 {page_type!r}，有效页型：{sorted(PAGE_TYPES)}")
    if "a" not in clients or "b" not in clients:
        raise ValueError("clients 需含 'a'（云 v6）与 'b'（云 VL）两键，'md' 可选")

    # -- 三通道转录（md 可选），按页号切片 -------------------------------
    page_a = pick_page(clients["a"].transcribe(file_path), page_no, "a(v6)")
    page_b = pick_page(clients["b"].transcribe(file_path), page_no, "b(VL)")
    md = clients.get("md")
    page_md = pick_page(md.transcribe(file_path), page_no, "md(vl-md)") if md is not None else None

    text_a = page_a.get("text_raw") or ""
    text_b = page_b.get("text_raw") or ""
    text_md = (page_md.get("text_raw") or "") if page_md is not None else None

    # -- 归一化（各面各配 FaceMap，实体检测与回填只在同面进行） ----------
    face_a = FaceMap.from_raw(text_a)
    face_b = FaceMap.from_raw(text_b)
    face_md = FaceMap.from_raw(text_md) if text_md is not None else None

    # -- 比对（归一化面）+ 三通道实体（regex+NER 逐面合并） --------------
    cmp_result = compare_transcripts(face_a.norm, face_b.norm, page_type)
    ents_a = merge_entities(extract_regex(face_a.norm, face_a),
                            extract_ner(face_a.norm, ner, face_a))
    ents_b = merge_entities(extract_regex(face_b.norm, face_b),
                            extract_ner(face_b.norm, ner, face_b))
    ents_md = None
    if face_md is not None:
        ents_md = merge_entities(extract_regex(face_md.norm, face_md),
                                 extract_ner(face_md.norm, ner, face_md))

    # -- 仲裁（R1-R7）→ 采信面判定 ---------------------------------------
    # verifiable_texts = 各面归一化转录（R6-rev 单方采信的逐字核验依据）
    verifiable = {"a": face_a.norm, "b": face_b.norm}
    if face_md is not None:
        verifiable["md"] = face_md.norm
    arb = arbitrate_page(cmp_result, ents_a, ents_b, ents_md, page_type, verifiable)
    need = _auto_face_requirements(arb, ents_a, ents_b)
    if need == {"b"}:  # 独有采信读数只在 b 面（R3 采 VL / R2-rev、R6-rev 单方采 b）
        text_gt, face_gt, ents_face = text_b, face_b, ents_b
    elif need <= {"a"}:
        text_gt, face_gt, ents_face = text_a, face_a, ents_a
    else:  # 双向各有独有采信面要求：构造上不可达（仲裁层两向独有不并采）
        raise ValueError(
            f"仲裁输出同时要求 a、b 两面承载独有采信读数（{sorted(need)}）"
            "——无单一采信面，管线不变量破坏")

    # -- 组装 pack（T6 契约形状；仲裁输出只读、结构全部新建） -------------
    models = ["v6", "vl"] + (["vl-md"] if md is not None else [])
    pack = {
        "page_id": f"{Path(file_path).stem}-p{page_no:03d}",
        "page_type": page_type,
        "source": {"file_sha256": _sha256_hex(file_path), "page": page_no,
                   "carrier": carrier, "segment": segment},
        "transcript_gt": {"text": text_gt, "normalized_text": face_gt.norm,
                          "fidelity": "machine"},
        "entities": _pack_entities(arb, ents_face),
        "adjudications": _pack_adjudications(arb, models),
    }

    # -- 自校验 gate：错误拒绝落盘（warning 放行、留日志） ----------------
    problems = validate_pagepack(pack)
    errors = [e for e in problems if not e.startswith("warning:")]
    for warning in (e for e in problems if e.startswith("warning:")):
        logger.warning("pagepack %s: %s", pack["page_id"], warning)
    if errors:
        raise ValueError(f"pagepack {pack['page_id']} 校验失败（未写出）：\n"
                         + "\n".join(errors))

    # -- 落盘 -------------------------------------------------------------
    out_dir = Path(work_dir) / _PACK_RELPATH / pack["page_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pack.json").write_text(
        json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    return pack
