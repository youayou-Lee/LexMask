"""
HaS (Hide And Seek) 本地模型客户端
使用 llama.cpp 的 OpenAI 兼容接口

推荐模型：xuanwulab/HaS_Text_0209_0.6B_Q4（GGUF：HaS_Text_0209_0.6B_Q4_K_M.gguf）
NER / Hide / Pair / Seek 的 user 提示须与模型卡模板逐字一致（勿在 NER 首段插入额外说明）。

功能：
1. ner - 敏感实体识别
2. hide - 标签化匿名化
3. pair - 提取标签映射
4. seek - 标签还原
"""

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

logger = logging.getLogger(__name__)
from dataclasses import dataclass
from typing import Any

from app.core.circuit_breaker import ner_breaker
from app.core.retry import RETRYABLE_HTTPX, retry_sync
from app.models.type_mapping import canonical_type_id

# 默认重试次数（max_retries 未显式传入时）
_DEFAULT_MAX_RETRIES = 2
# 重试退避基准秒数
_RETRY_BASE_DELAY_SEC = 1.0
# 采样温度（确定性输出）
_MODEL_TEMPERATURE = 0.0
# 采样 top_p
_MODEL_TOP_P = 0.6
# max_tokens 下限（_call_model 单次请求）
_MIN_CALL_MAX_TOKENS = 32
# 提示词 token 估算（Issue#104 C2 校准）：按字符类别取**语料经验上界**系数
# （单位 = token/字符，浮点直读，禁止再引入整数倍刻度——D2b 首轮曾因 ×2/×20
# 刻度混用把估算放大 10 倍，引发硬切死循环）。实测锚点（云 SCNet 回归表）：
#   纯中文 ≈ 0.69 token/字符 ⇒ 上界取 1.0；
#   数字长串 ≈ 1.02 ⇒ 取 1.05；
#   拉丁交替大小写 ≈ 0.75 ⇒ 取 0.7（兼顾问模板英文说明的固定计价,纯中文长行比值 ≤1.55）；
#   星平面字符（emoji/CJK 扩展 B）≈ 3~4 ⇒ 取 4.0（独立类）；
#   其余 ASCII 0.5、控制符 1.0。
# 「永不高估」由云端 D2b 回归表裁决（estimate ≥ usage.prompt_tokens 逐行）。
_PROMPT_TOKENS_PER_DIGIT = 1.05
_PROMPT_TOKENS_PER_LATIN = 0.7
_PROMPT_TOKENS_PER_ASTRAL = 4.0
_PROMPT_TOKENS_PER_OTHER_ASCII = 0.5
_PROMPT_TOKENS_PER_NON_ASCII = 1.0
_PROMPT_TOKENS_PER_CONTROL = 1.0
_PROMPT_TOKEN_OVERHEAD = 32
# 上下文探测（Issue#104 C1）：按 base_url 缓存服务端自报 max_model_len 的 TTL
_CONTEXT_PROBE_TTL_SEC = 300.0
# NER 类型批量目标 token 数下限
_NER_TYPE_BATCH_TARGET_TOKENS_FLOOR = 320
# 由目标 token 推导每批类型数的除数
_NER_BATCH_TOKENS_PER_TYPE = 40
# 每批类型数下限
_NER_BATCH_MIN_TYPES = 8
# 每批类型数上限
_NER_BATCH_MAX_TYPES = 24
# 单趟 NER 文本字符数下限
_NER_SINGLE_PASS_MIN_CHARS = 128
# NER 期望 max_tokens 下限
_NER_DESIRED_MAX_TOKENS_FLOOR = 640
# NER 期望 max_tokens：每个类型预算 token 数（模块默认；生效值读
# settings.HAS_NER_TOKENS_PER_TYPE，S1 校准默认 160，见 config.py 注释）
_NER_TOKENS_PER_TYPE = 160
# NER 期望 max_tokens：文本字符折算除数
_NER_TEXT_CHARS_PER_TOKEN = 2
# NER 上下文窗口 token 数下限
_NER_CONTEXT_TOKENS_FLOOR = 512
# 上下文中预留给模板/分隔的 token 数
_NER_CONTEXT_RESERVE_TOKENS = 160
# 补全 token 上限下限
_NER_COMPLETION_CAP_FLOOR = 384
# 补全 token 占上下文窗口比例
_NER_COMPLETION_CONTEXT_RATIO = 0.65
# 受上下文余量约束后的 max_tokens 下限
_NER_MAX_TOKENS_FLOOR = 64
# 触发分批回退的类型数阈值
_NER_REBATCH_TYPE_THRESHOLD = 24
# 类型引导 description 最大字符数
_TYPE_GUIDANCE_DESC_MAX_CHARS = 96
# 健康检查结果缓存秒数
_HEALTH_CHECK_CACHE_SEC = 5.0
# 健康检查请求超时秒数
_HEALTH_CHECK_TIMEOUT_SEC = 5.0


# ---------------------------------------------------------------------------
# 截断风暴观测（Issue#41）
# ---------------------------------------------------------------------------
# _call_model 把 finish_reason 写入当前上下文；ner() 读取后记入激活的
# NerMetricsScope。scope 栈用 ContextVar：异步任务内（含 to_thread 拷贝的
# 上下文）嵌套叠加、跨线程/跨任务天然隔离，并发页互不串。

LAST_FINISH_REASON: ContextVar[str | None] = ContextVar(
    "has_ner_last_finish_reason", default=None
)
_METRIC_SCOPES: ContextVar[tuple] = ContextVar("has_ner_metric_scopes", default=())


class NerMetricsScope:
    """一段 NER 调用区间的观测累积器（stage 层开一个，finally 时 drain）。

    线程安全：finish_reasons 用 list.append（GIL 原子）；计数用锁（verify
    段 gather+to_thread 多线程并发 += 会丢计，评审 Minor-2）。
    """

    __slots__ = ("finish_reasons", "truncation_retries", "rebatch_batches", "_lock")

    def __init__(self) -> None:
        self.finish_reasons: list[str] = []
        self.truncation_retries = 0
        self.rebatch_batches = 0
        self._lock = threading.Lock()


def _record_ner_metrics(
    *,
    finish_reason: str | None = None,
    truncation_retries: int = 0,
    rebatch_batches: int = 0,
) -> None:
    scopes = _METRIC_SCOPES.get()
    if not scopes:
        return
    for scope in scopes:
        if finish_reason:
            scope.finish_reasons.append(finish_reason)
        with scope._lock:
            scope.truncation_retries += int(truncation_retries)
            scope.rebatch_batches += int(rebatch_batches)


@contextmanager
def ner_metrics_scope():
    """Open a NER metrics scope; drain with drain_ner_metrics() on exit."""
    scope = NerMetricsScope()
    token = _METRIC_SCOPES.set(_METRIC_SCOPES.get() + (scope,))
    try:
        yield scope
    finally:
        _METRIC_SCOPES.reset(token)


def drain_ner_metrics(scope: NerMetricsScope) -> dict[str, Any]:
    """Scope → stage 指标键（观测面口径，评审 Important-4 后定稿）：

    - has_text_ner_finish_reason / has_text_ner_truncation_retries：
      **全路径观测键**——只要有模型调用/重查就写（off 模式同样产出，供
      "缺报=FAIL"双向判定与现状截断基线度量）；
    - has_text_ner_rebatch_batches：**帽路径专属键**——帽驱动分批才产出，
      帽=0（现状模式）结构上不写（e2e-1 空转防线依据）。
    """
    metrics: dict[str, Any] = {}
    if scope.finish_reasons:
        metrics["has_text_ner_finish_reason"] = (
            "length" if "length" in scope.finish_reasons else "stop"
        )
    if scope.truncation_retries > 0:
        metrics["has_text_ner_truncation_retries"] = scope.truncation_retries
    if scope.rebatch_batches > 0:
        metrics["has_text_ner_rebatch_batches"] = scope.rebatch_batches
    return metrics


@dataclass
class HaSEntity:
    """HaS识别的实体"""
    text: str
    type: str
    tag: str | None = None  # 结构化语义标签


@dataclass
class HaSResult:
    """HaS处理结果"""
    original_text: str
    masked_text: str
    entities: dict[str, list[str]]  # {类型: [实体列表]}
    mapping: dict[str, list[str]]   # {标签: [原文列表]}


def _classify_ner_error(e: BaseException) -> str:
    """Issue#104 C5：兜底异常 → NER_ERRORS{error_type} 的分类标签。"""
    if isinstance(e, httpx.HTTPStatusError):
        status = getattr(e.response, "status_code", 0) or 0
        if status == 400:
            return "http_400"
        if status == 422:
            return "http_422"
        if status >= 500:
            return "http_5xx"
        return f"http_{status}"
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    if isinstance(e, (httpx.ConnectError, httpx.NetworkError)):
        return "network"
    return "other"


class HaSClient:
    """HaS本地模型客户端"""

    _SHARED_NER_CACHE: OrderedDict[
        tuple[str, tuple[str, ...], str, str, int],
        tuple[float, dict[str, list[str]]],
    ] = OrderedDict()
    _SHARED_NER_INFLIGHT: dict[tuple[str, tuple[str, ...], str, str, int], threading.Event] = {}
    _SHARED_NER_LOCK = threading.Lock()
    # Issue#104 C1：服务端自报上下文的探测缓存（base_url → ( monotonic, tokens )）。
    # 类级共享：同进程所有 client 实例对同一端点只探一次（TTL 见模块常量）。
    _context_probe_cache: dict[str, tuple[float, int]] = {}
    _context_probe_lock = threading.Lock()


    def __init__(
        self,
        base_url: str = None,
        timeout: float = None,
        max_retries: int | None = None,
    ):
        from app.core.config import settings
        self._base_url_override = base_url.rstrip("/") if base_url else None
        self.timeout = httpx.Timeout(timeout or settings.HAS_TIMEOUT)
        self._max_retries = _DEFAULT_MAX_RETRIES if max_retries is None else max(0, int(max_retries))
        self._ner_cache_ttl_sec = settings.HAS_NER_CACHE_TTL_SEC
        self._ner_cache_max_items = settings.HAS_NER_CACHE_MAX_ITEMS
        self._ner_cache = self._SHARED_NER_CACHE
        self._ner_inflight = self._SHARED_NER_INFLIGHT
        self._ner_lock = self._SHARED_NER_LOCK
        # 复用 httpx 连接池，避免每次请求创建新连接
        self._http_client = httpx.Client(timeout=self.timeout, trust_env=False)
        self._health_checked_at = 0.0
        self._health_ready = False

    def _effective_base_url(self) -> str:
        from app.core.config import get_has_chat_base_url
        if self._base_url_override:
            return self._base_url_override.rstrip("/")
        return get_has_chat_base_url().rstrip("/")

    def _probe_server_context(self, base_url: str) -> int | None:
        """GET {base}/models → 该链自报的 max_model_len（Issue#104 C1）。

        按 HAS_TEXT_MODEL_NAME 选条目（多模型端点各链上下文不同）；任何失败
        （不可达/超时/非 JSON/缺字段/≤0）都返回 None，由调用方回落安全小值。
        """
        wanted = ""
        try:
            from app.core.config import settings

            wanted = str(getattr(settings, "HAS_TEXT_MODEL_NAME", "") or "").strip()
        except Exception:
            wanted = ""
        url = f"{base_url.rstrip('/')}/models"
        try:
            resp = self._http_client.get(url, timeout=_HEALTH_CHECK_TIMEOUT_SEC)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.debug("HaS context probe failed for %s: %s", url, exc)
            return None
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return None
        selected: dict[str, Any] | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or "").strip()
            if wanted:
                if entry_id == wanted:
                    selected = entry
                    break
            elif selected is None:
                selected = entry
        if selected is None:
            return None
        try:
            value = int(selected.get("max_model_len") or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _effective_context_tokens(self) -> int:
        """生效上下文 = min(服务端自报值, 配置值)；探测失败回落安全小值（C1）。

        回落值 = min(HAS_NER_CONTEXT_TOKENS, HAS_NER_CONTEXT_FALLBACK_TOKENS)
        （默认 8192 与 4096 ⇒ 4096）。**禁止**回落到更大的数：4096 链上回落
        8192 = 原样退回被 400 的旧病。探测按 base_url 缓存（TTL 300s），首错
        清缓存；失败打 WARNING 但**不拒服**。同步 httpx——只允许在 worker 线程
        （ner 本就在 to_thread 里）或经 asyncio.to_thread 调用，不得进事件循环。
        """
        from app.core.config import settings

        configured = max(_NER_CONTEXT_TOKENS_FLOOR, int(settings.HAS_NER_CONTEXT_TOKENS))
        base_url = self._effective_base_url()
        now = time.monotonic()
        with self._context_probe_lock:
            cached = self._context_probe_cache.get(base_url)
        if cached is not None and now - cached[0] <= _CONTEXT_PROBE_TTL_SEC:
            return min(cached[1], configured)
        probed_raw: Any = self._probe_server_context(base_url)
        try:
            probed = int(probed_raw)
        except (TypeError, ValueError):
            probed = 0
        if probed <= 0:
            with self._context_probe_lock:
                self._context_probe_cache.pop(base_url, None)  # 首错清缓存
            fallback = min(
                configured,
                max(_NER_CONTEXT_TOKENS_FLOOR, int(settings.HAS_NER_CONTEXT_FALLBACK_TOKENS)),
            )
            logger.warning(
                "HaS context probe failed for %s; falling back to %d tokens (min of configured=%d and fallback)",
                base_url,
                fallback,
                configured,
            )
            return fallback
        effective = min(probed, configured)
        with self._context_probe_lock:
            self._context_probe_cache[base_url] = (now, effective)
        return effective

    def _do_chat_request(self, base: str, payload: dict[str, Any]) -> httpx.Response:
        """Execute a single chat completions HTTP request (retryable, uses pooled client)."""
        def _request():
            resp = self._http_client.post(f"{base}/chat/completions", json=payload)
            resp.raise_for_status()
            return resp
        return ner_breaker.call_sync(_request)

    def _call_model(
        self,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """调用 OpenAI 兼容接口（llama.cpp HaS）。

        temperature 未传时用确定性默认 _MODEL_TEMPERATURE(=0.0)；自洽多趟采样时
        调用方按趟传入(第0趟贪心0.0，后续 temp>0)。
        """
        from app.core.config import settings
        base = self._effective_base_url()
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": _MODEL_TEMPERATURE if temperature is None else float(temperature),
            "top_p": _MODEL_TOP_P,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max(_MIN_CALL_MAX_TOKENS, int(max_tokens))
        if settings.HAS_TEXT_MODEL_NAME:
            payload["model"] = settings.HAS_TEXT_MODEL_NAME
        started = time.perf_counter()
        response = retry_sync(
            self._do_chat_request, base, payload,
            max_retries=self._max_retries, base_delay=_RETRY_BASE_DELAY_SEC,
            retryable_exceptions=RETRYABLE_HTTPX,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        logger.info("HaS model request finished in %dms", elapsed_ms)
        data = response.json()
        # 安全访问嵌套结构，避免 KeyError/IndexError
        choices = data.get("choices")
        if not choices or not isinstance(choices, list) or len(choices) == 0:
            logger.error("HaS 模型返回无 choices: %.200s", str(data))
            LAST_FINISH_REASON.set("none")
            return ""
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        LAST_FINISH_REASON.set(finish_reason or "none")
        if finish_reason == "length":
            logger.warning(
                "HaS model response was truncated by max_tokens=%s",
                payload.get("max_tokens"),
            )
        elif finish_reason and finish_reason not in {"stop", "eos_token"}:
            logger.info("HaS model finish_reason=%s", finish_reason)
        message = choice.get("message", {})
        return message.get("content", "")

    @staticmethod
    def _copy_ner_result(result: dict[str, list[str]]) -> dict[str, list[str]]:
        return {
            key: list(value) if isinstance(value, list) else value
            for key, value in result.items()
        }

    @staticmethod
    def _try_parse_json_object(input_text: str) -> tuple[str, dict[str, Any] | None]:
        raw = str(input_text or "").strip()
        if not raw:
            return "empty", None

        fenced = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE).strip()
        fenced = re.sub(r"\s*```$", "", fenced).strip()
        start = fenced.find("{")
        end = fenced.rfind("}")

        candidates: list[tuple[str, str]] = [("direct", raw)]
        if fenced != raw:
            candidates.append(("fenced", fenced))
        if start >= 0 and end > start:
            candidates.append(("substring", fenced[start:end + 1]))

        seen: set[str] = set()
        for mode, candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                return mode, parsed

        try:
            from json_repair import repair_json
        except Exception:
            return "failed", None

        for mode, candidate in candidates:
            if not candidate:
                continue
            try:
                repaired = repair_json(candidate, return_objects=False)
                parsed = json.loads(repaired)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return f"json_repair:{mode}", parsed

        return "failed", None

    @staticmethod
    def _trim_truncated_tail_value(result: dict[str, Any]) -> dict[str, Any]:
        """Drop the cut-off tail value json_repair leaves in the last bucket.

        A repetition loop truncated by max_tokens ends mid-value; the repaired
        JSON keeps that fragment (e.g. '2') as a real value. Only drop it when
        it is a proper prefix of an earlier value in the same bucket — the
        signature of a truncated repetition, never of a legitimate value.
        """
        if not result:
            return result
        last_key = next(reversed(result))
        values = result.get(last_key)
        if not isinstance(values, list) or len(values) < 2:
            return result
        tail = values[-1]
        if isinstance(tail, str) and tail and any(
            isinstance(value, str) and value != tail and value.startswith(tail)
            for value in values[:-1]
        ):
            result = dict(result)
            result[last_key] = values[:-1]
        return result

    @staticmethod
    def _normalize_ner_type_name(type_name: str) -> str:
        value = str(type_name or "").strip()
        if not value:
            return ""
        if value.isascii():
            return canonical_type_id(value.upper())

        return value

    @staticmethod
    def _effective_tokens_per_type(settings: Any) -> int:
        """生效的每类型完成 token 成本（预算与容量反推两处共用，防漂移）。

        显式配置一律尊重（含设回 72 复现 preview 预算）；未配置/非法值取
        模块常量（S1 校准 160）。HAS_NER_TOKENS_PER_TYPE=160 是新全局基线：
        帽=0 只关闭分批，不改预算公式（off ≠ preview 预算，评审 Important-3）。
        """
        raw = getattr(settings, "HAS_NER_TOKENS_PER_TYPE", None)
        if raw is None:
            return _NER_TOKENS_PER_TYPE
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return _NER_TOKENS_PER_TYPE
        return value if value > 0 else _NER_TOKENS_PER_TYPE

    @staticmethod
    def _ner_completion_budget(types_count: int, text: str, per_type: int) -> int:
        """期望完成 token 预算（与下方 desired_max_tokens 同口径）。

        Issue#41 主动分批的密度判据：该预算超过有效完成帽的整页调用会被
        max_tokens 截断（复读病理页例外见 #60），须前置沿类型轴分批。
        """
        return max(
            _NER_DESIRED_MAX_TOKENS_FLOOR,
            types_count * per_type + len(str(text or "")) // _NER_TEXT_CHARS_PER_TOKEN,
        )

    @staticmethod
    def _effective_completion_hard_cap(settings: Any) -> int:
        """有效完成帽。≤0 = 关闭（退回现状：预算照算、帽不约束）。"""
        try:
            return max(0, int(getattr(settings, "HAS_NER_COMPLETION_HARD_CAP", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def plan_completion_batches(
        cls,
        types: list[str] | None,
        text: str,
        *,
        hard_cap: int | None = None,
        per_type: int | None = None,
    ) -> list[list[str]]:
        """Issue#41 帽驱动分批决策；Issue#103 上移为可复用的**纯函数**入口。

        为什么需要这个入口：`ner` 里的容量分批是**同步串行**的，而且整串跑在
        调用方已经占住的那一个 `shared_gpu_inference_slot` 里（见 `has_service.
        extract_entities`），所以闸门抬再高也覆盖不到闸位内那几次子调用。把
        「怎么切」这件事抽成本函数后，异步服务层可以在**派发前**先切好，交给
        既有的 `asyncio.gather` + 信号量并发跑，每个子批各取一个闸位。

        返回值：`[list(types)]`（单批）表示"整页单次调用"；多批表示前置分批。
        判据与 `ner` 内完全同源（同一个 capacity 公式），因此调用方预切之后，
        `ner` 内部再切一次是**幂等**的（预切批长 ≤ capacity → 不再切），
        直接调 `ner` 的旧调用方（如视觉线 `has_text_analysis`）行为零变化。
        """
        from app.core.config import settings

        items = list(types or [])
        if not items:
            return []
        # 显式入参优先（便于单测直接钉死帽值，不必 monkeypatch 全局 settings）；
        # 缺省仍读全局配置，保持生产调用点零改动。
        if hard_cap is None:
            hard_cap = cls._effective_completion_hard_cap(settings)
        if hard_cap <= 0:
            return [items]
        if per_type is None:
            per_type = cls._effective_tokens_per_type(settings)
        if per_type <= 0:
            # 显式传 0/负数时退化为"不按类型预留"，避免除零；调用方若真要关帽
            # 应传 hard_cap<=0（上面已短路）。
            per_type = _NER_TOKENS_PER_TYPE
        text_budget = len(str(text or "")) // _NER_TEXT_CHARS_PER_TOKEN
        desired_budget = cls._ner_completion_budget(len(items), text, per_type)
        capacity = (hard_cap - text_budget) // per_type
        if desired_budget <= hard_cap or len(items) <= 1 or capacity < 1:
            # 预算装得下 / 单类型 / 页面超长豁免：整页单次
            return [items]
        if len(items) <= capacity:
            return [items]
        return [items[i : i + capacity] for i in range(0, len(items), capacity)]

    @staticmethod
    def _estimate_prompt_tokens(text: str) -> int:
        """Conservative local estimate for vLLM context budgeting.

        Issue#104：按字符类别取**语料经验上界**系数（token/字符）：
        星平面 `_PROMPT_TOKENS_PER_ASTRAL`=4.0、控制符 1.0、数字
        `_PROMPT_TOKENS_PER_DIGIT`=1.05、拉丁字母 `_PROMPT_TOKENS_PER_LATIN`
        =0.7、其它 ASCII 0.5、非 ASCII 1.0，再加固定模板开销
        `_PROMPT_TOKEN_OVERHEAD`=32。

        这不是可证上界：emoji 代理对、CJK 扩展 B、RTL、组合符仍可能低估；
        「永不高估」由云端 D2b 语料回归表裁决（estimate ≥
        usage.prompt_tokens 逐行，2026-10-16 扩展版 51 行低估 0），凡回归集
        出现低估的类别先上调该类系数再重跑。已知高估方向：高度重复的合成串
        （全大写循环、重复 emoji、无分隔长串）分词压缩明显，比值可达 2–6×，
        代价只是可能多分组。
        """
        tokens = 0.0
        for char in str(text or ""):
            code = ord(char)
            if code > 0xFFFF:  # 星平面（emoji / CJK 扩展 B）：分词 3~4 token/字符
                tokens += _PROMPT_TOKENS_PER_ASTRAL
            elif code < 32:  # 控制符（\n 等）
                tokens += _PROMPT_TOKENS_PER_CONTROL
            elif code < 128:
                if char.isdigit():
                    tokens += _PROMPT_TOKENS_PER_DIGIT
                elif char.isalpha():
                    tokens += _PROMPT_TOKENS_PER_LATIN
                else:
                    tokens += _PROMPT_TOKENS_PER_OTHER_ASCII
            else:
                tokens += _PROMPT_TOKENS_PER_NON_ASCII
        return max(1, int(tokens) + _PROMPT_TOKEN_OVERHEAD)

    @staticmethod
    def _build_ner_prompt(types: list[str], guidance_text: str, text: str) -> str:
        """与 HaS_Text_0209 模型卡 NER 模板一致（Specified types: 与 JSON 数组之间无空格）。

        ner() 实发与分组规划器（Issue#104 C3）共用本函数——估算/预算只允许
        一处算术，两处手写必然漂移。
        """
        types_str = json.dumps(list(types), ensure_ascii=False, separators=(",", ":"))
        guidance_block = f"\nType guidance:{guidance_text}" if guidance_text else ""
        return f"""Recognize the following entity types in the text.
Specified types:{types_str}
{guidance_block}
Return strict JSON only. Include only entity types that have matches in the text.
Never output empty arrays. Do not return requested types with no matches. Do not explain.
If nothing matches, return {{}}.
<text>{text}</text>"""

    @classmethod
    def _resolve_ner_budget(
        cls,
        types: list[str],
        text: str,
        *,
        context_tokens: int,
        prompt: str | None = None,
        guidance_text: str = "",
    ) -> tuple[str, int, int]:
        """NER 提示词 + 完成预算的**唯一一份算术**（Issue#104 C3；冷审 I1）。

        `ner_checked()` 的实发与分组规划器 `planned_max_tokens()` 都必须走这里。
        旧实现是两处手抄的 min 链——本 Issue 的根因正是「规划一套、实发一套」的
        两套算术漂移，故此处不允许再有副本。

        Returns ``(prompt, prompt_token_estimate, max_tokens)``。
        """
        from app.core.config import settings

        if prompt is None:
            prompt = cls._build_ner_prompt(list(types), guidance_text, str(text or ""))
        configured_max_tokens = int(settings.HAS_NER_MAX_TOKENS)
        desired_max_tokens = cls._ner_completion_budget(
            len(types), text, cls._effective_tokens_per_type(settings)
        )
        prompt_token_estimate = cls._estimate_prompt_tokens(prompt)
        context_room = context_tokens - prompt_token_estimate - _NER_CONTEXT_RESERVE_TOKENS
        completion_cap = max(
            _NER_COMPLETION_CAP_FLOOR,
            min(configured_max_tokens, int(context_tokens * _NER_COMPLETION_CONTEXT_RATIO)),
        )
        max_tokens = min(
            configured_max_tokens,
            desired_max_tokens,
            completion_cap,
            max(_NER_MAX_TOKENS_FLOOR, context_room),
        )
        hard_cap = cls._effective_completion_hard_cap(settings)
        if hard_cap > 0:
            max_tokens = min(max_tokens, hard_cap)
        return prompt, prompt_token_estimate, int(max_tokens)

    @classmethod
    def planned_max_tokens(
        cls,
        types: list[str],
        text: str,
        *,
        context_tokens: int | None = None,
        guidance_text: str = "",
    ) -> int:
        """给定 (类型, 文本) 的完成预算——**ner() 实发 max_tokens 的同一算术**。

        Issue#104 C3：与 `ner_checked()` 共用 `_resolve_ner_budget`，消除
        「规划一套、实发一套」的两套算术。context_tokens 缺省用配置值；
        调方（规划器）应传入客户端探测到的生效上下文。
        """
        if context_tokens is None:
            from app.core.config import settings

            context_tokens = max(_NER_CONTEXT_TOKENS_FLOOR, int(settings.HAS_NER_CONTEXT_TOKENS))
        return cls._resolve_ner_budget(
            list(types),
            text,
            context_tokens=int(context_tokens),
            guidance_text=guidance_text,
        )[2]

    @classmethod
    def _normalize_ner_types(cls, entity_types: list[str] | None) -> list[str]:
        if not entity_types:
            raise ValueError("entity_types 必须显式传入非空清单(清单即真相源,无隐藏默认词表)")
        return list(dict.fromkeys(
            normalized
            for type_name in entity_types
            if (normalized := cls._normalize_ner_type_name(type_name))
        ))

    def _ner_cache_key(
        self,
        text: str,
        entity_types: list[str],
        guidance_key: str = "",
        sample_index: int = 0,
    ) -> tuple[str, tuple[str, ...], str, str, int]:
        # sample_index keeps self-consistent temp>0 passes over the SAME payload
        # from being served the temp=0 seed's cached result (union would collapse
        # to a single 趟). Default 0 = the deterministic seed / today's key.
        digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        return (
            self._effective_base_url(),
            tuple(sorted(entity_types)),
            digest,
            guidance_key,
            int(sample_index),
        )

    def _ner_cache_key_for(
        self,
        text: str,
        types: list[str],
        guidance_text: str = "",
        sample_index: int = 0,
    ) -> tuple[str, tuple[str, ...], str, str, int]:
        """缓存键的**单点**推导（Issue#108）。

        `ner()`（写侧）与 `set_cached_ner()`（Issue#108 新增的公开写回侧）都必须走这里。
        一旦两处的键公式漂移，表现是「写进去了却永远读不中」——不报错、不告警，
        只能靠人肉比对 `_ner_cache` 的键，所以键公式只允许有一个出口。
        """
        guidance_key = (
            hashlib.sha256(guidance_text.encode("utf-8", errors="ignore")).hexdigest()
            if guidance_text
            else ""
        )
        return self._ner_cache_key(text, types, guidance_key, sample_index)

    def _get_cached_ner(
        self,
        key: tuple[str, tuple[str, ...], str, str, int],
    ) -> dict[str, list[str]] | None:
        if self._ner_cache_ttl_sec <= 0 or self._ner_cache_max_items <= 0:
            return None
        now = time.monotonic()
        with self._ner_lock:
            cached = self._ner_cache.get(key)
            if not cached:
                return None
            stored_at, result = cached
            if now - stored_at > self._ner_cache_ttl_sec:
                self._ner_cache.pop(key, None)
                return None
            self._ner_cache.move_to_end(key)
            logger.info("HaS NER cache hit")
            return self._copy_ner_result(result)

    def get_cached_ner(
        self,
        text: str,
        entity_types: list[str] | None = None,
    ) -> dict[str, list[str]] | None:
        """Return a cached NER result without starting a model request.

        Vision recognition uses a process-level HaS Text lock to avoid sending
        multiple expensive llama.cpp requests at once. Cache reads are local
        memory operations, so callers can use this method before waiting on
        that lock and avoid a stale duplicate page blocking behind an unrelated
        slow request.
        """
        types = self._normalize_ner_types(entity_types)
        return self._get_cached_ner(self._ner_cache_key_for(text, types))

    def set_cached_ner(
        self,
        text: str,
        entity_types: list[str] | None,
        result: dict[str, list[str]],
        type_guidance: list[dict[str, Any]] | None = None,
        *,
        sample_index: int = 0,
    ) -> None:
        """按**完整类型表**写回缓存条目（`get_cached_ner` 的写侧，Issue#108）。

        Issue#103 把「帽容量分批」从 `ner()` 内部上移到
        `has_service._preplan_completion_batches` 之后，`ner()` 只会被以**子批**类型表
        调用，于是只写子批键、不再写全类型键；以完整类型表探测缓存的调用方
        （视觉线 `has_text_analysis` → `has_client.get_cached_ner`）因此复用不到
        混合链同一份文本的结果，同一段文字被重复推了两遍。

        ⚠️ 本方法**无条件写入**：调用方必须先在调用点确认**所有**子批都成功。
        `asyncio.gather(return_exceptions=True)` 吞掉的失败批若被合并后写进缓存，
        该键在 TTL 内会一直返回**偏少**的结果——表现为永久性漏检，且没有任何日志。
        """
        types = self._normalize_ner_types(entity_types)
        guidance = self._normalize_type_guidance(types, type_guidance)
        guidance_text = json.dumps(guidance, ensure_ascii=False, separators=(",", ":")) if guidance else ""
        key = self._ner_cache_key_for(text, types, guidance_text, sample_index)
        self._set_cached_ner(key, result)

    def _set_cached_ner(
        self,
        key: tuple[str, tuple[str, ...], str, str, int],
        result: dict[str, list[str]],
    ) -> None:
        if self._ner_cache_ttl_sec <= 0 or self._ner_cache_max_items <= 0:
            return
        with self._ner_lock:
            self._ner_cache[key] = (time.monotonic(), self._copy_ner_result(result))
            self._ner_cache.move_to_end(key)
            while len(self._ner_cache) > self._ner_cache_max_items:
                self._ner_cache.popitem(last=False)

    def _begin_ner_request(
        self,
        key: tuple[str, tuple[str, ...], str, str, int],
    ) -> tuple[bool, threading.Event | None]:
        if self._ner_cache_ttl_sec <= 0 or self._ner_cache_max_items <= 0:
            return True, None
        with self._ner_lock:
            event = self._ner_inflight.get(key)
            if event is not None:
                logger.info("HaS NER joined in-flight duplicate request")
                return False, event
            event = threading.Event()
            self._ner_inflight[key] = event
            return True, event

    def _finish_ner_request(
        self,
        key: tuple[str, tuple[str, ...], str, str, int],
        event: threading.Event | None,
    ) -> None:
        if event is None:
            return
        with self._ner_lock:
            self._ner_inflight.pop(key, None)
            event.set()

    def create_session_mapping(self) -> dict[str, list[str]]:
        """创建一个独立的会话映射（用于并发安全的批处理）。

        调用方应在请求开始时创建，然后传给 hide() 的 mapping 参数，
        这样每个请求拥有独立的映射，不会互相污染。
        """
        return {}

    def ner(
        self,
        text: str,
        entity_types: list[str] | None = None,
        type_guidance: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        sample_index: int = 0,
        _allow_truncation_retry: bool = True,
        raise_on_error: bool = False,
        context_tokens: int | None = None,
    ) -> dict[str, list[str]]:
        """
        使用NER能力进行敏感实体识别

        Args:
            text: 待识别文本
            entity_types: 要识别的实体类型清单(必须显式传入)
            temperature: 本次请求采样温度。None=确定性默认(0.0)。自洽多趟采样时
                调用方按趟传(第0趟贪心0.0，后续 temp>0)。
            sample_index: 自洽采样趟号，进缓存键。默认 0=贪心种子/现状键；>0 的趟
                与种子不共享缓存，避免 temp>0 趟被喂种子结果导致并集塌成 1 趟。
            raise_on_error: Issue#104 C5——True 时在计数 NER_ERRORS 后**上抛**而非
                返回 {}。分组派发（主查）用它：组级失败必须让页判失败，不得
                伪装成「无实体」静默通过。
            context_tokens: Issue#104 C3/冷审 I1——调用方钉住的生效上下文。分组
                派发把**规划时**用的值透传进来，保证规划与实发同源（None=自行
                探测，行为与旧版一致）。

        Returns:
            {类型: [实体列表]}

        ⚠️ 返回值不含「这次到底有没有拿到结论」的信息：HTTP 错/超时/输出不可解析
        全被吞成 `{}`，与「模型合法地回答这页没有实体」在返回值上完全同形。
        需要该判据（例如决定要不要把结果写进缓存）的调用方请用 `ner_checked()`。
        """
        return self.ner_checked(
            text,
            entity_types,
            type_guidance,
            temperature=temperature,
            sample_index=sample_index,
            _allow_truncation_retry=_allow_truncation_retry,
            raise_on_error=raise_on_error,
            context_tokens=context_tokens,
        )[0]

    def ner_checked(
        self,
        text: str,
        entity_types: list[str] | None = None,
        type_guidance: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        sample_index: int = 0,
        _allow_truncation_retry: bool = True,
        raise_on_error: bool = False,
        context_tokens: int | None = None,
    ) -> tuple[dict[str, list[str]], bool]:
        """`ner()` + **显式解析成功标志**（Issue#108 ⑥ 评审 C1/C2）。

        返回 `(结果, ok)`。`ok=True` 表示本次请求的**每一个**类型都落在一份
        **解析成功**的模型响应里；`ok=False` 表示至少一个类型的结论只是「没拿到」
        （HTTP 错/超时被吞、输出不可解析、截断修复后整表缺失）。

        为什么必须显式给：`ner()` 把失败吞成 `{}`，与合法的空答案在返回值上无法
        区分，而 `asyncio.gather(return_exceptions=True)` 也看不到被吞掉的异常
        （`isinstance(batch_result, Exception)` 分支永不进入）。写侧若拿「没抛异常」
        当成功判据，部分并集/空结果会污染缓存键并在 TTL 内固化成永久漏检
        （评审已复现：C1 部分并集 12/19 桶、C2 整页 0 实体固化）。

        判据是**解析成功**，不是**结果非空**：提示词明确「无匹配就回 `{}`」，所以
        合法空对象算成功（否则合法空页会被反复重推）。
        """
        types = self._normalize_ner_types(entity_types)
        guidance = self._normalize_type_guidance(types, type_guidance)
        guidance_text = json.dumps(guidance, ensure_ascii=False, separators=(",", ":")) if guidance else ""
        cache_key = self._ner_cache_key_for(text, types, guidance_text, sample_index)
        cached = self._get_cached_ner(cache_key)
        if cached is not None:
            # 命中即视为成功——依赖不变量：**缓存里的条目恒为「已确认解析成功」**。
            # 所以任何 ok=False 的结果都不得落键（⑥ 二审 Critical-1 实证：残缺结果
            # 一旦落键，下一次同键请求就从这里被洗成 ok=True，服务层的写回门随即
            # 失效——零模型调用即可把部分并集提升成全类型键）。
            return cached, True

        from app.core.config import settings

        # ----- Issue#41 完成帽治理（帽驱动，无论是否带 guidance 都生效） -----
        # S1 实测（2026-10-06，DCU 实例）两件事：
        # ① 原公式 72 token/型对密集 CJK 页低估 ~2.4 倍，批被自己的 max_tokens
        #   腰斩（12 型批 2032 截断、重查 1960/1528 仍截断）——与模型卡帽无关；
        # ② 拆批越细模型每类型产出越臃肿（38 型整页人均 ~103 token，拆 5 型/批
        #   人均 ~394）——整页一次问又快又全（spike+arm5 实证），预分批在该类
        #   payload 上从根上就是反效果。
        # 因此：帽 >0 时由帽接管分批决策——预算 ≤ 帽 → 整页单次调用（跳过 legacy
        # 预分批）；预算 > 帽 → 按帽容量反推批大小前置分批；单类型仍超帽的页豁免
        # （分批无解，单发走既有截断补救）。帽 =0 → 关闭分批接管（legacy 预分批），
        # 预算公式仍用 HAS_NER_TOKENS_PER_TYPE 基线（=新全局基线，≠preview 72）。
        hard_cap = self._effective_completion_hard_cap(settings)
        if hard_cap > 0:
            # 容量 = 帽减去文本份额后每类型可容批大小；<1（页长到连单类型都
            # 超帽）→ 页面级豁免：整页单发走既有截断补救（评审 Important-1，
            # chunk=1 悬崖——拆成 N 个仍截断的串行调用零收益纯浪费）。
            # Issue#103：切分方案改由 plan_completion_batches 单点给出（同一
            # capacity 公式），避免"调用方预切"与"客户端内切"两处公式漂移；
            # 判据与原先逐字等价（desired<=cap / 单类型 / capacity<1 → 单批）。
            planned_batches = self.plan_completion_batches(types, text)
            if len(planned_batches) > 1:
                guidance_by_type = {str(item.get("type") or ""): item for item in (type_guidance or [])}
                merged: dict[str, list[str]] = {}
                all_confirmed = True
                for batch in planned_batches:
                    batch_guidance = (
                        [guidance_by_type[t] for t in batch if t in guidance_by_type] or None
                    )
                    part, part_confirmed = self.ner_checked(
                        text,
                        batch,
                        batch_guidance,
                        temperature=temperature,
                        sample_index=sample_index,
                        raise_on_error=raise_on_error,
                        context_tokens=context_tokens,
                    )
                    all_confirmed = all_confirmed and part_confirmed
                    for key, values in (part or {}).items():
                        if not values:
                            continue
                        bucket = merged.setdefault(key, [])
                        for value in values:
                            if value not in bucket:
                                bucket.append(value)
                _record_ner_metrics(rebatch_batches=len(planned_batches))
                # Issue#108 ⑥ 评审 C1/C2：全类型键是各子批的**并集**，只有每个子批
                # 都解析成功时它才是完整结论。若有子批只是「没拿到」，写进去的部分
                # 并集会被读侧（视觉线只探全类型键）当成命中，在 TTL 内固化成漏检。
                if all_confirmed:
                    self._set_cached_ner(cache_key, merged)
                return merged, all_confirmed
            # desired ≤ 帽（或单类型）→ 整页单次：不落入 legacy 预分批

        if hard_cap <= 0 and type_guidance is None:
            configured_batch = max(1, int(settings.HAS_NER_MAX_TYPES_PER_REQUEST))
            target = max(_NER_TYPE_BATCH_TARGET_TOKENS_FLOOR, int(settings.HAS_NER_TYPE_BATCH_TARGET_TOKENS))
            target_batch = max(_NER_BATCH_MIN_TYPES, min(_NER_BATCH_MAX_TYPES, target // _NER_BATCH_TOKENS_PER_TYPE))
            batch_size = max(1, min(configured_batch, target_batch))
            single_pass_max_types = max(batch_size, int(settings.HAS_NER_SINGLE_PASS_MAX_TYPES))
            single_pass_max_chars = max(_NER_SINGLE_PASS_MIN_CHARS, int(settings.HAS_NER_SINGLE_PASS_MAX_TEXT_CHARS))
            should_single_pass = (
                len(types) <= single_pass_max_types
                and len(str(text or "")) <= single_pass_max_chars
            )
            if len(types) > batch_size and not should_single_pass:
                logger.info(
                    "HaS NER pre-batching %d types into chunks of %d",
                    len(types),
                    batch_size,
                )
                merged: dict[str, list[str]] = {}
                all_confirmed = True
                for start in range(0, len(types), batch_size):
                    part, part_confirmed = self.ner_checked(
                        text,
                        types[start:start + batch_size],
                        None,
                        temperature=temperature,
                        sample_index=sample_index,
                        raise_on_error=raise_on_error,
                        context_tokens=context_tokens,
                    )
                    all_confirmed = all_confirmed and part_confirmed
                    for key, values in (part or {}).items():
                        if not values:
                            continue
                        bucket = merged.setdefault(key, [])
                        for value in values:
                            if value not in bucket:
                                bucket.append(value)
                # Issue#108 ⑥ 评审 C1/C2：同「帽驱动预分批」——并集只有在每个子批都
                # 解析成功时才是完整结论，否则不得写全类型键。
                if all_confirmed:
                    self._set_cached_ner(cache_key, merged)
                return merged, all_confirmed

        owns_request, request_event = self._begin_ner_request(cache_key)
        if not owns_request:
            if request_event is not None:
                request_event.wait(timeout=self.timeout.read or None)
            cached = self._get_cached_ner(cache_key)
            if cached is not None:
                return cached, True
            # 同 payload 的并发请求跑完却没留下缓存 ⇒ 它也没拿到确认结论。
            logger.warning("HaS NER duplicate request finished without cacheable result")
            return {}, False

        # 与 HaS_Text_0209 模型卡 NER 模板一致（Specified types: 与 JSON 数组之间
        # 无空格）。prompt 构造与预算算术另有唯一出口 `_resolve_ner_budget`
        # （Issue#104 C3；冷审 I1）：ner() 实发与分组规划器共用同一份，不得另写。
        # Issue#104 C1：上下文真源 = 服务端自报值（/v1/models），探测失败回落
        # 安全小值 min(配置, 4096)——旧口径全局 8192 在 4096 链上照样 400。
        # 分组派发会把规划时钉住的上下文透传进来（冷审 I1）：同一页的规划与
        # 实发必须用同一个值，否则 TTL 到期重探会让两者漂移。
        resolved_context = (
            int(context_tokens)
            if context_tokens is not None
            else self._effective_context_tokens()
        )
        prompt, prompt_token_estimate, max_tokens = self._resolve_ner_budget(
            types,
            text,
            context_tokens=resolved_context,
            guidance_text=guidance_text,
        )
        context_tokens = resolved_context
        logger.info(
            "HaS NER budget prompt_est=%d context=%d max_tokens=%d types=%d text_chars=%d",
            prompt_token_estimate,
            context_tokens,
            max_tokens,
            len(types),
            len(text),
        )

        messages = [
            {
                "role": "user",
                "content": prompt
            }
        ]

        try:
            started = time.perf_counter()
            reason_token = LAST_FINISH_REASON.set(None)
            try:
                response = self._call_model(messages, max_tokens=max_tokens, temperature=temperature)
                finish_reason = LAST_FINISH_REASON.get()
            finally:
                LAST_FINISH_REASON.reset(reason_token)
            if finish_reason:
                _record_ner_metrics(finish_reason=str(finish_reason))
            parse_mode, result = self._try_parse_json_object(response)
            if result is None:
                logger.warning("HaS NER response could not be parsed as JSON: %.200s", response)
                if type_guidance is None and len(types) > _NER_REBATCH_TYPE_THRESHOLD:
                    from app.core.config import settings
                    target = max(_NER_TYPE_BATCH_TARGET_TOKENS_FLOOR, int(settings.HAS_NER_TYPE_BATCH_TARGET_TOKENS))
                    batch_size = max(_NER_BATCH_MIN_TYPES, min(_NER_BATCH_MAX_TYPES, target // _NER_BATCH_TOKENS_PER_TYPE))
                    merged: dict[str, list[str]] = {}
                    all_confirmed = True
                    for start in range(0, len(types), batch_size):
                        part, part_confirmed = self.ner_checked(
                            text,
                            types[start:start + batch_size],
                            None,
                            temperature=temperature,
                            sample_index=sample_index,
                            raise_on_error=raise_on_error,
                            context_tokens=context_tokens,
                        )
                        all_confirmed = all_confirmed and part_confirmed
                        for key, values in (part or {}).items():
                            if not values:
                                continue
                            bucket = merged.setdefault(key, [])
                            for value in values:
                                if value not in bucket:
                                    bucket.append(value)
                    # 三处写回门统一为「全部确认即写」（⑥ 二审 Minor-B）：并集为空
                    # 只表示「每个子批都合法地答了无实体」，那也是完整结论。
                    if all_confirmed:
                        self._set_cached_ner(cache_key, merged)
                    return merged, all_confirmed
                # Issue#108 ⑥ 评审 C1/C2：整段输出不可解析 ≠ 模型答「无实体」，
                # 必须报 ok=False，否则这个空结果会被写侧当成完整结论缓存起来。
                return {}, False
            logger.info(
                "HaS NER parsed %d type buckets via %s in %dms",
                len(result),
                parse_mode,
                round((time.perf_counter() - started) * 1000),
            )
            confirmed = True
            # ⑥ 二审 Important-4：解析成功 ≠「回答了请求类型」。提示词要求桶名取
            # 请求类型；一份**非空**却一个请求类型都不含的回答是契约违背（模型编了
            # 别的桶名），落键后读侧命中即把请求类型在 TTL 内永久跳过 ⇒ 不算成功。
            # 合法空答案（`{}`，提示词明确「无匹配就回 {}」）不在此列。
            if result and not any(t in result for t in types):
                confirmed = False
            if parse_mode.startswith("json_repair"):
                # 输出被截断/损坏（如日期桶复读循环耗尽 max_tokens）：
                # ①末桶的截断残值丢弃；②整桶丢失的请求类型补查一次并合并。
                result = self._trim_truncated_tail_value(result)
                if not _allow_truncation_retry:
                    # 补查层（调用方正在补查）自己也被腰斩：修复出的残缺结果不可信。
                    confirmed = False
                else:
                    missing = [t for t in types if t not in result]
                    if len(missing) == len(types):
                        # 修复后的结果不含任何请求类型：重查 = 同 payload 同预算
                        # 原样重发（必然同结局），且补查 key 与本调用相同——会
                        # 自连接本调用尚未释放的 inflight 事件、白等满超时
                        # （e2e 复现：批调用 3×120s 挂起）。返回修复的部分结果。
                        # Issue#108 ⑥ 一审：这一支不改（e2e 加固过的死锁规避路径），
                        # 但 ok=False；⑥ 二审 Critical-1/2：**并且不得写缓存**。
                        # 落键的后果是「洗白」——下次同键请求从不变量分支命中并报
                        # ok=True，服务层写回门失效，零模型调用就能把 12/19 的部分
                        # 并集写进全类型键（二审实测 RUN1 门生效、RUN2 写穿）；单批
                        # 形态下这条键还与读侧探测键重合，`{}` 直接被读侧判命中。
                        # 死锁规避只要求「不自我补查」，不要求「写缓存」。
                        logger.warning(
                            "HaS NER output recovered via %s but lacks every requested type; skipping self-retry",
                            parse_mode,
                        )
                        return result, False
                    if missing:
                        _record_ner_metrics(truncation_retries=1)
                        logger.warning(
                            "HaS NER output recovered via %s; re-querying %d missing types: %s",
                            parse_mode,
                            len(missing),
                            missing,
                        )
                        missing_set = set(missing)
                        retry_guidance = [
                            item for item in (type_guidance or [])
                            if str(item.get("type") or "") in missing_set
                        ] or None
                        retry_result, retry_confirmed = self.ner_checked(
                            text,
                            missing,
                            retry_guidance,
                            temperature=temperature,
                            sample_index=sample_index,
                            _allow_truncation_retry=False,
                            raise_on_error=raise_on_error,
                            context_tokens=context_tokens,
                        )
                        for key, values in (retry_result or {}).items():
                            if not isinstance(values, list):
                                continue
                            bucket = result.setdefault(key, [])
                            for value in values:
                                if value and value not in bucket:
                                    bucket.append(value)
                        confirmed = retry_confirmed
            # Issue#108 ⑥ 评审 C1/C2：只有确认过解析成功的结果才进缓存；残缺结果
            # 仍原样返回给调用方（尽力而为的容忍语义不变），但不落键。
            if confirmed:
                self._set_cached_ner(cache_key, result)
            return result, confirmed
        except Exception as e:
            # Issue#104 C5：不再静默——按错误类型计数（此前 redaction_ner_errors_total
            # 声明了但全仓零引用），日志带上估算/长度/上下文，让「少识别」可归因。
            from app.core.metrics import NER_ERRORS

            error_type = _classify_ner_error(e)
            try:
                NER_ERRORS.labels(error_type=error_type).inc()
            except Exception:
                logger.debug("NER_ERRORS metric unavailable", exc_info=True)
            logger.error(
                "HaS NER 失败(%s): %s | prompt_est=%d text_chars=%d context=%d",
                error_type,
                e,
                prompt_token_estimate,
                len(text or ""),
                context_tokens,
            )
            if raise_on_error:
                raise
            return {}, False
        finally:
            self._finish_ner_request(cache_key, request_event)

    @staticmethod
    def _normalize_type_guidance(
        types: list[str],
        type_guidance: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if not type_guidance:
            return []
        allowed = set(types)
        normalized: list[dict[str, Any]] = []
        for item in type_guidance:
            type_name = str(item.get("type") or "").strip()
            if not type_name or type_name not in allowed:
                continue
            description = re.sub(r"\s+", " ", str(item.get("description") or "").strip())
            entry: dict[str, Any] = {"type": type_name}
            if description:
                entry["description"] = description[:_TYPE_GUIDANCE_DESC_MAX_CHARS]
            normalized.append(entry)
        return normalized

    def hide(
        self,
        text: str,
        entity_types: list[str] | None = None,
        use_history: bool = True,
        mapping: dict[str, list[str]] | None = None,
    ) -> tuple[str, dict[str, list[str]]]:
        """
        使用Hide能力进行标签化匿名化

        流程：
        1. 先调用NER识别实体
        2. 再调用Hide替换为结构化标签

        Args:
            text: 待匿名化文本
            entity_types: 要识别的实体类型
            use_history: 是否使用已有映射（保持指代一致性）
            mapping: 请求级映射字典，由调用方通过 create_session_mapping()
                     创建并在同一请求内复用。若为 None 则每次创建新的空映射。

        Returns:
            (匿名化后文本, 映射表)
        """
        types = self._normalize_ner_types(entity_types)
        types_str = json.dumps(types, ensure_ascii=False, separators=(",", ":"))

        # 使用调用方传入的映射，或创建一个请求局部的空映射
        session_mapping: dict[str, list[str]] = mapping if mapping is not None else {}

        # Step 1: NER识别
        ner_result = self.ner(text, types)
        if not ner_result or all(len(v) == 0 for v in ner_result.values()):
            return text, {}

        ner_json = json.dumps(ner_result, ensure_ascii=False)

        # Step 2: Hide 第 1 轮与 ner() 使用相同 NER 模板
        ner_prompt = f"""Recognize the following entity types in the text.
Specified types:{types_str}
<text>{text}</text>"""

        if use_history and session_mapping:
            # 带历史映射
            history_json = json.dumps(session_mapping, ensure_ascii=False)
            messages = [
                {
                    "role": "user",
                    "content": ner_prompt
                },
                {
                    "role": "assistant",
                    "content": ner_json
                },
                {
                    "role": "user",
                    "content": f"Replace the above-mentioned entity types in the text according to the existing mapping pairs:{history_json}"
                }
            ]
        else:
            # 不带历史映射
            messages = [
                {
                    "role": "user",
                    "content": ner_prompt
                },
                {
                    "role": "assistant",
                    "content": ner_json
                },
                {
                    "role": "user",
                    "content": "Replace the above-mentioned entity types in the text."
                }
            ]

        try:
            masked_text = self._call_model(messages)

            # Step 3: 提取映射
            new_mapping = self.pair(text, masked_text)

            # 将新映射合并到 session_mapping（调用方持有同一引用）
            for tag, values in new_mapping.items():
                if tag not in session_mapping:
                    session_mapping[tag] = []
                for v in values:
                    if v not in session_mapping[tag]:
                        session_mapping[tag].append(v)

            return masked_text, new_mapping

        except Exception as e:
            logger.error("HaS Hide 失败: %s", e)
            return text, {}

    def pair(self, original_text: str, masked_text: str) -> dict[str, list[str]]:
        """
        使用Pair能力提取标签映射

        Args:
            original_text: 原始文本
            masked_text: 匿名化后文本

        Returns:
            {标签: [原文列表]}
        """
        messages = [
            {
                "role": "user",
                "content": f"""<original>{original_text}</original>
<anonymized>{masked_text}</anonymized>
Extract the mapping from anonymized entities to original entities."""
            }
        ]

        try:
            response = self._call_model(messages)
            result = json.loads(response)
            return result
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except (json.JSONDecodeError, ValueError):
                    pass
            return {}
        except Exception as e:
            logger.error("HaS Pair 失败: %s", e)
            return {}

    def seek(self, masked_text: str, mapping: dict[str, list[str]] | None = None) -> str:
        """
        使用Seek能力进行标签还原

        Args:
            masked_text: 匿名化后的文本
            mapping: 映射表（必须由调用方显式提供）

        Returns:
            还原后的原文
        """
        if not mapping:
            return masked_text

        mapping_json = json.dumps(mapping, ensure_ascii=False)

        messages = [
            {
                "role": "user",
                "content": f"""The mapping from anonymized entities to original entities:
{mapping_json}
Restore the original text based on the above mapping:
{masked_text}"""
            }
        ]

        try:
            restored_text = self._call_model(messages)
            return restored_text
        except Exception as e:
            logger.error("HaS Seek 失败: %s", e)
            return masked_text

    @staticmethod
    def _is_live_health_payload(data: dict[str, Any]) -> bool:
        status = str(data.get("status", "")).strip().lower()
        if status in {"busy", "running", "processing", "inferencing", "loading", "starting", "warming_up", "warming-up"}:
            return True
        if status in {"unavailable", "offline", "degraded", "error", "failed"}:
            return False
        return bool(data["ready"]) if "ready" in data else True

    def is_available(self) -> bool:
        """检查 NER 后端是否可用（llama.cpp /v1/models）。"""
        from app.core.config import get_has_health_check_url
        from app.core.health_checks import _tcp_port_open
        url = get_has_health_check_url()
        now = time.monotonic()
        if now - self._health_checked_at < _HEALTH_CHECK_CACHE_SEC:
            return self._health_ready
        try:
            response = self._http_client.get(url, timeout=_HEALTH_CHECK_TIMEOUT_SEC)
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:
                    data = {}
                self._health_ready = self._is_live_health_payload(data) if isinstance(data, dict) else True
            elif response.status_code == 503 and _tcp_port_open(url):
                self._health_ready = True
            else:
                self._health_ready = False
            self._health_checked_at = now
            return self._health_ready
        except httpx.TimeoutException:
            if _tcp_port_open(url):
                self._health_ready = True
                self._health_checked_at = now
                return True
            self._health_ready = False
            self._health_checked_at = now
            return False
        except Exception:
            self._health_ready = False
            self._health_checked_at = now
            return False


# 全局客户端实例
has_client = HaSClient()
