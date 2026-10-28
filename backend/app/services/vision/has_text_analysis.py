"""HaS NER text analysis over OCR blocks.

Split out of ocr_pipeline.py (which stays the public facade): whole-document
HaS NER with cache/in-flight dedupe, the reconstructed-line bridge pass and
the structural table-amount / form-field recall merges.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from collections import Counter
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.services.has_client import NerDeadlineExceeded
from app.services.ocr_has_vision_service import OCRTextBlock
from app.services.vision.has_text_payload import (
    _build_has_text_payload,
    _build_has_text_type_names,
    _canonical_image_text_type,
    _compact_text,
    _default_has_text_items,
    _filter_blocks_for_has_text,
    _item_query_labels,
    plan_has_text_groups,
)
from app.services.vision.ocr_cache import (
    _add_has_text_duration,
    _begin_has_text_ner_inflight,
    _finish_has_text_ner_inflight,
    _get_cached_has_text_ner,
    _has_recent_negative_health,
    _has_text_ner_inflight_key,
    _record_has_text_metric,
)
from app.services.vision.ocr_entity_match import _is_isolated_token_occurrence
from app.services.vision.ocr_table_semantics import _block_search_text
from app.services.vision.ocr_tuning import (
    _BRIDGE_PAYLOAD_MAX_CHARS,
    _NER_DEFAULT_MIN_LEN,
    _NER_MIN_LEN_BY_TYPE,
)
from app.services.vision.ocr_visual_lines import reconstruct_visual_line_blocks

logger = logging.getLogger(__name__)


# NER runs on the whole document, not per-line chunks: feeding the 0.6B model a
# context-free single cell (a lone "汉族") makes it force-fit that value into the
# nearest requested type when its true type isn't in the schema (民族 absent ->
# "汉族" lands under 性别). With the full page it has the context to assign 男->性别
# and leave 汉族 out — 找到就找到，找不到就没有。Tradeoff: the model dilutes recall on a
# long page and may drop an entity near the very end (e.g. a standalone signature
# date). Reordering/chunking to recover it reintroduces the force-fit, because this
# model classifies sequentially — order and recall are coupled. Dates were once
# force-recalled by a 年月日 regex backstop (both digit systems); a 5/5 reliability
# probe — including the CJK-digit "一九五八年九月十日" the backstop was written for —
# showed HaS now returns every date deterministically, so the regex was deleted.
# The model judges what is a date; no hand-written pattern enumerates them.


def _merge_ner_into_union(
    union: dict[str, list[str]], result: Any
) -> bool:
    """Fold one NER pass into the running union; return whether it added anything.

    Order-preserving, dedup within a type bucket. A bucket is only created when
    a real (non-empty) value lands in it, so an all-empty pass never plants a
    spurious empty type. Returns True iff at least one new (type,value) pair was
    added — the caller reads that only as a budget-convergence signal.
    """
    if not isinstance(result, dict):
        return False
    added = False
    for entity_type, values in result.items():
        if not isinstance(values, list):
            continue
        for value in values:
            if not value:
                continue
            bucket = union.setdefault(entity_type, [])
            if value not in bucket:
                bucket.append(value)
                added = True
    return added


async def _resolve_effective_context(has_client: Any) -> int:
    """生效上下文（Issue#104 C1）：真客户端走服务端探测（worker 线程内）；
    鸭子类型的最小桩（测试替身无该方法）回落配置值——探测缺失不拒服。"""
    probe = getattr(has_client, "_effective_context_tokens", None)
    if callable(probe):
        return await asyncio.to_thread(probe)
    return max(512, int(settings.HAS_NER_CONTEXT_TOKENS))


def merge_group_ner_results(group_results: list[Any]) -> dict[str, list[str]]:
    """跨组合并 {type: [values]}：保序、组间 (type, value) 去重（Issue#104 D1⑥）。

    与既有 bridge 合并同款去重语义；空桶不落键。组级失败由调用方先行处置，
    本函数只吃成功的 dict。
    """
    merged: dict[str, list[str]] = {}
    for result in group_results:
        if not isinstance(result, dict):
            continue
        for entity_type, entity_list in result.items():
            if not entity_list:
                continue
            bucket = merged.setdefault(entity_type, [])
            for value in entity_list:
                clean_text = _compact_text(value)
                if clean_text and clean_text not in bucket:
                    bucket.append(clean_text)
    return merged


def plan_has_text_group_texts(
    texts: list[str],
    chinese_types: list[str],
    *,
    context_tokens: int,
) -> list[str]:
    """把整页文本行按 token 预算分成若干组文本（Issue#104 C3 接线）。

    estimate/预算闭包直接用 HaSClient 的真函数（staticmethod/classmethod，
    不依赖实例状态）——与实发**同一算术**，鸭子类型的测试桩也无感。
    返回组文本列表；单组时与现状逐字等价。
    """
    from app.services.has_client import _NER_CONTEXT_RESERVE_TOKENS, HaSClient

    estimate_for = lambda t: HaSClient._estimate_prompt_tokens(  # noqa: E731
        HaSClient._build_ner_prompt(chinese_types, "", t)
    )
    max_tokens_for = lambda t: HaSClient.planned_max_tokens(  # noqa: E731
        chinese_types, t, context_tokens=context_tokens
    )
    groups = plan_has_text_groups(
        texts,
        context_tokens=context_tokens,
        reserve_tokens=_NER_CONTEXT_RESERVE_TOKENS,
        estimate_for=estimate_for,
        max_tokens_for=max_tokens_for,
    )
    return ["\n".join(group) for group in groups]


async def _run_has_text_ner_groups(
    has_client: Any,
    stage_status: dict[str, Any] | None,
    group_texts: list[str],
    chinese_types: list[str],
    *,
    context_tokens: int | None = None,
    deadline: float | None = None,
) -> tuple[dict[str, list[str]], int]:
    """逐组派发 HaS 主查（Issue#104 C3/C5）。

    - 每组独立走 缓存 → inflight（按**组文本**键）→ 组并发窗口 + GPU 闸位；
      单组时组文本 == 整页文本，与现状逐字等价（C6：同一 payload、同一闸位
      数、同一 K 趟自洽采样）。
    - 组并发窗口 = HAS_VISION_MAX_GROUPS_INFLIGHT（独立于 GPU 闸门的硬界：
      闸门被 SERIALIZE_SHARED_GPU_MODELS=False 旁路时扇出仍有界，D5②）。
    - 组内 ner(raise_on_error=True)：组失败**上抛**而非返回 {}，页级据此判
      失败——组级静默漏检（本 Issue 的病）不得在组上复发（C5/D-cm）。
    - context_tokens：规划时钉住的生效上下文，透传给 ner()（冷审 I1）——
      否则 ner() 自行重探，TTL 到期后规划与实发会漂移到两套上下文。
    - deadline（Issue#209）：页级 NER 预算的单调钟死线。耗尽后**不再开新组**
      （在飞组跑完即可），并把 `has_text_ner_budget_exceeded` /
      `has_text_ner_budget_skipped_groups` 记进 stage_status——返回 None 的组
      既不进合并、也不算失败组（失败组语义是"该页判失败"，预算语义是"该页
      只扫了一部分"，由调用方按 HAS_NER_PAGE_BUDGET_STRICT_FAIL 决定是否落成
      页级失败）。默认 None = 无预算，与历史逐字等价。

    Returns ``(merged_union, failed_group_count)``；失败组不参与合并（调用方
    据此整页判失败）。
    """
    from app.core.gpu_inference_gate import shared_gpu_inference_slot

    k_samples = max(1, int(settings.HAS_NER_SELF_CONSIST_SAMPLES))
    sample_temp = float(settings.HAS_NER_SELF_CONSIST_TEMPERATURE)
    group_sem = asyncio.Semaphore(
        max(1, min(int(settings.HAS_VISION_MAX_GROUPS_INFLIGHT), len(group_texts)))
    )

    def _budget_skip() -> None:
        """记一次「因预算耗尽而跳过一组」。

        Issue#209：返回 None 而不是 {}——{} 会被下游当成"这组合法地答无实体"
        （C5 的病），None 则被 ``merge_group_ner_results`` 与失败过滤天然排除。
        """
        _record_has_text_metric(stage_status, "has_text_ner_budget_exceeded", 1)
        _add_has_text_duration(stage_status, "has_text_ner_budget_skipped_groups", 1)
        logger.warning(
            "HaS page NER budget exhausted (deadline=%s); skipping remaining group(s)",
            deadline,
        )

    async def _run_group(group_text: str) -> dict[str, list[str]] | None:
        if deadline is not None and time.monotonic() >= deadline:
            # 进队时已超预算：不再开新组（在飞组跑完即可）。
            _budget_skip()
            return None
        cached = _get_cached_has_text_ner(has_client, group_text, chinese_types)
        if cached is not None:
            return cached
        inflight_key = _has_text_ner_inflight_key(has_client, group_text, chinese_types)
        owns_inflight, inflight_future = _begin_has_text_ner_inflight(inflight_key)
        if not owns_inflight:
            # 同组文本的兄弟页：等 owner 落定（与旧整页 inflight 语义一致）。
            return await asyncio.shield(inflight_future)
        result: dict[str, list[str]] | None = None
        budget_skipped = False
        try:
            async with group_sem:
                # 排队等槽位/等 GPU 闸也可能把预算耗掉——必须在**真发请求之前**
                # 复检，否则"停发剩余组"只是队列长度决定的运气。
                if deadline is not None and time.monotonic() >= deadline:
                    _budget_skip()
                    budget_skipped = True
                else:
                    queue_start = time.perf_counter()
                    async with shared_gpu_inference_slot("OCR HaS Text NER"):
                        queue_ms = round((time.perf_counter() - queue_start) * 1000)
                        _add_has_text_duration(stage_status, "has_text_slot_wait_ms", queue_ms)
                        cached = _get_cached_has_text_ner(has_client, group_text, chinese_types)
                        if cached is not None:
                            _record_has_text_metric(stage_status, "has_text_cache_status", "hit_after_slot")
                            result = cached
                        else:
                            model_start = time.perf_counter()

                            def _ner_sample(pass_index: int) -> Any:
                                return has_client.ner(
                                    group_text,
                                    chinese_types,
                                    temperature=(0.0 if pass_index == 0 else sample_temp),
                                    sample_index=pass_index,
                                    raise_on_error=True,
                                    context_tokens=context_tokens,
                                    deadline=deadline,
                                )

                            # Issue#209 C：预算必须是**硬墙**。上面两处复检只在
                            # 阶段边界，而单次 ner() 内部还可能走「截断→按缺失类型
                            # 切 9~12 段补查→残差重发」的长链（页 133 实测 160s+，
                            # 病态组合 300s+），门④ 实测该页因此仍然撞 300s 页墙、
                            # 预算一次都没触发。这里用剩余预算 wait_for 兜住整条链，
                            # 超时即丢该组（partial 语义与跳过一致，strict 时由
                            # 调用方抛 HasTextNerPageBudgetExceeded）。
                            remaining = (
                                None
                                if deadline is None
                                else deadline - time.monotonic()
                            )
                            if remaining is not None and remaining <= 0:
                                _budget_skip()
                                budget_skipped = True
                            else:
                                try:
                                    if remaining is None:
                                        result, passes_run = await asyncio.to_thread(
                                            aggregate_ner_samples, _ner_sample, k_samples
                                        )
                                    else:
                                        # 硬墙要点：`asyncio.to_thread` 里的模型调用
                                        # 在同步 HTTP 里跑，取消不了；`wait_for` 会
                                        # **等它自然结束**再抛 TimeoutError（实测 2s
                                        # 预算量出 3.05s），等于没有墙。用 asyncio.wait
                                        # 超时就直接放手（线程在后台跑完，其结果无人
                                        # 消费；页不再被它拖住）。
                                        task = asyncio.ensure_future(
                                            asyncio.to_thread(
                                                aggregate_ner_samples, _ner_sample, k_samples
                                            )
                                        )
                                        done, _pending = await asyncio.wait(
                                            {task}, timeout=remaining
                                        )
                                        if task not in done:
                                            task.add_done_callback(
                                                lambda t: t.cancelled() or t.exception()
                                            )
                                            raise TimeoutError
                                        result, passes_run = task.result()
                                except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
                                    _budget_skip()
                                    budget_skipped = True
                                except NerDeadlineExceeded:
                                    # 补查链在死线处拒绝继续发请求 ⇒ 该组只拿到部分
                                    # 结论，按预算跳过语义处理（不当作组失败）。
                                    _budget_skip()
                                    budget_skipped = True
                                else:
                                    _record_has_text_metric(
                                        stage_status, "has_text_cache_status", "model_call"
                                    )
                                    _add_has_text_duration(
                                        stage_status,
                                        "has_text_self_consist_passes",
                                        passes_run,
                                    )
                                    _add_has_text_duration(
                                        stage_status,
                                        "has_text_model_ms",
                                        round((time.perf_counter() - model_start) * 1000),
                                    )
            if budget_skipped:
                # 不填 {}（那会被当成"该组答无实体"），填 None 让等待方同样判跳过。
                _finish_has_text_ner_inflight(inflight_key, inflight_future, None)
                return None
            _finish_has_text_ner_inflight(inflight_key, inflight_future, result)
            return result if isinstance(result, dict) else {}
        except BaseException as exc:
            # 失败也必须通知 inflight 的等待方：填异常而非 None，否则兄弟页/兄弟组
            # 会拿到非异常的 None 而被判成功，按半结果继续（冷审 Critical）。
            _finish_has_text_ner_inflight(inflight_key, inflight_future, None, error=exc)
            raise

    results = await asyncio.gather(
        *(_run_group(group_text) for group_text in group_texts),
        return_exceptions=True,
    )
    failures = [item for item in results if isinstance(item, BaseException)]
    for failure in failures:
        logger.error("HaS text NER group raised: %s", failure)
    merged = merge_group_ner_results([item for item in results if isinstance(item, dict)])
    return merged, len(failures)


def aggregate_ner_samples(
    sample_fn: Callable[[int], Any],
    max_passes: int,
) -> tuple[dict[str, list[str]], int]:
    """Self-consistent multi-pass NER union — pure core; the GPU loop is the caller's.

    ``sample_fn(index)`` yields pass ``index``'s raw NER result (type->values).
    Pass 0 MUST be the temp=0 greedy seed and passes >=1 temp>0 re-samples —
    the caller wires temperature/sample-index per index (HaSClient.ner takes
    both; the sample-index cache key keeps a temp>0 pass from being served the
    seed's cached result and collapsing the union to a single 趟). This function
    only owns the union + seed + stopping arithmetic.

    Leak-safety (0 new leaks):
      * The union is monotone non-decreasing — values only ever accumulate, so
        it is always a SUPERSET of pass 0, i.e. of today's deterministic output.
      * Convergence ("this pass added no new value") is ONLY a budget signal to
        stop spending GPU — never evidence the page is clean. It is checked only
        for passes after the seed, and it never rolls back the union already
        gathered.
      * Hallucinated values sampled at temp>0 stay in the union; the downstream
        matcher is the leak-safe gate — it yields 0 region for any value that
        does not occur in the OCR blocks. Growing the union cannot uncover PII.

    接线 TODO (human GPU main loop): drive ``sample_fn`` as
    ``lambda i: has_client.ner(text, types, temperature=(0.0 if i == 0 else T),
    sample_index=i)`` inside shared_gpu_inference_slot; read ``max_passes`` from
    a config knob; cache ONLY the returned union under the canonical
    (sample_index=0) key for downstream reuse. Whether temp>0 sampling actually
    lifts recall, and whether it saturates in 2-4 passes without EngineDead
    bursts, is for real-GPU verification — not asserted here.

    Returns ``(union, passes_run)``.
    """
    if max_passes < 1:
        max_passes = 1
    union: dict[str, list[str]] = {}
    passes_run = 0
    for index in range(max_passes):
        result = sample_fn(index)
        passes_run += 1
        added = _merge_ner_into_union(union, result)
        # index 0 is the temp=0 seed (its find is the union floor). Only a
        # post-seed pass that adds nothing signals convergence -> stop.
        if index > 0 and not added:
            break
    return union, passes_run


# Digits and Chinese numerals/magnitudes — the count part of a money amount. Used to
# stop amount-value narrowing from mistaking a dropped leading numeral for context.
_AMOUNT_NUMERAL_CHARS = frozenset("0123456789零〇一二三四五六七八九十百千万亿兆两壹贰叁肆伍陆柒捌玖拾佰仟萬億")


async def _narrow_amount_entities(entities: list[dict[str, str]], has_client: Any) -> None:
    """Model-driven AMOUNT value narrowing（人民币每亩每年100元 → 100元）.

    The sensitive ink on the page is the value token, but HaS queried as 金额
    returns the span WITH its business context — that is its training
    semantics, not an error. Queried as 数值 (settings.AMOUNT_VALUE_QUERY_LABEL)
    on the entity text alone, the same model extracts the value itself, so the
    narrowing is the model's own judgment end to end — no token grammar, no
    word lists. One value that is a proper substring -> the span narrows to
    it. SEVERAL values (a dual-numeral span such as '人民币陆7元整(￥360000元)'
    — the 大写 reading plus the bracketed figure, 0712 房屋合同实证) -> the
    entity SPLITS into one AMOUNT entity per value, each hunting its own box;
    the old single-value-only rule silently dropped every extra numeral
    (360000 went unmasked). No answer / model failure keeps the whole span,
    so narrowing still only ever trims label context, never uncovers a value.
    """
    label = str(getattr(settings, "AMOUNT_VALUE_QUERY_LABEL", "") or "").strip()
    if not label or not has_client:
        return
    # Each per-entity re-ask goes through the global GPU gate, serialized like
    # every other local model call. No concatenated batching: a shared context
    # makes HaS return a shorter wrong substring of one value (欠盖), so每实体
    # 独立一次、闸上串行。The slot is released between entities so unrelated
    # pipeline GPU work can interleave.
    from app.core.gpu_inference_gate import shared_gpu_inference_slot

    split_entities: list[dict[str, str]] = []
    for entity in entities:
        if entity.get("type") != "AMOUNT":
            continue
        original = str(entity.get("text") or "")
        if not original:
            continue
        try:
            async with shared_gpu_inference_slot("OCR HaS AMOUNT value narrow"):
                result = await asyncio.to_thread(has_client.ner, original, [label])
        except Exception as exc:
            logger.debug("amount value narrowing skipped for %r: %s", original, exc)
            continue
        values = [str(v).strip() for v in (result or {}).get(label, []) if str(v).strip()]
        values = list(dict.fromkeys(v for v in values if v != original and v in original))
        # A SINGLE narrow that is a SUFFIX of the original but whose dropped LEADING
        # prefix carries a numeral did NOT strip business context — it dropped part of
        # the number itself (拾万元 -> 万元 loses the 拾, 100,000 -> an ambiguous 万). Keep
        # the whole span rather than uncover the dropped digit. A context strip (人民币每
        # 亩每年100元 -> 100元, prefix has no numeral) narrows; a multi-value SPLIT (壹佰万元
        # / 100元) is left intact — there the prefix numeral belongs to the OTHER value.
        if (
            len(values) == 1
            and original.endswith(values[0])
            and any(c in _AMOUNT_NUMERAL_CHARS for c in original[: -len(values[0])])
        ):
            continue
        if not values:
            continue
        logger.debug("amount narrowed by model: %r -> %r", original, values)
        entity["text"] = values[0]
        for extra in values[1:]:
            split_entities.append({**entity, "text": extra})
    entities.extend(split_entities)


def _block_residual_ink(block_compact: str, consumed_values: list[str]) -> str | None:
    """The block's value-level residual ink, or None when the block is consumed.

    Replaces the old bare-containment identity (any(value in block or block in
    value)) — which let 消费=["张三"] swallow the block 负责人张三丰 and let one
    recalled ID mark a merged two-column 身份证号码 block fully consumed, so the
    second column's ID never reached the residual re-ask.

    A block is consumed when the model has already explained every value-sized
    span of its ink:

      - the whole block is a fragment of a returned value (block ⊂ value —
        the original second branch, kept verbatim); or
      - carving out every returned value's ISOLATED-TOKEN occurrences
        (_is_isolated_token_occurrence: script-class change / punctuation /
        string edge — so 张三 does not consume 张三丰) leaves no contiguous
        uncovered run as long as half the shortest value that anchored in this
        block. That half-of-shortest-anchor scale is the block's own document
        self-calibration — never a hardcoded char count: a bare field label
        left over (号码：) falls below it and does not resurrect the block,
        while a second, unexplained value (the other column's ID) clears it.

    Otherwise the uncovered characters (in order) are returned as the residual
    ink to re-ask — the consumed sibling value and the field labels are gone,
    so the residual differs from the full block text and clears the
    residual_text != text_content gate even for a lone merged two-column block.
    """
    if not block_compact:
        return None
    # Second branch (kept): the whole block is a piece of some returned value.
    for value in consumed_values:
        if value and block_compact in value:
            return None
    covered = [False] * len(block_compact)
    anchored_lens: list[int] = []
    for value in consumed_values:
        if not value:
            continue
        start = block_compact.find(value)
        while start != -1:
            end = start + len(value)
            if _is_isolated_token_occurrence(block_compact, start, end):
                for i in range(start, end):
                    covered[i] = True
                anchored_lens.append(len(value))
            start = block_compact.find(value, start + 1)
    leftover = "".join(ch for ch, is_cov in zip(block_compact, covered, strict=False) if not is_cov)
    if not leftover:
        return None
    if not anchored_lens:
        # No returned value anchored here — the block is wholly unexplained.
        return leftover
    # A leftover run at least half as long as the shortest anchored value is
    # another value hiding in the block; a leftover made only of sub-value
    # fragments (field labels) is treated as already explained.
    scale = min(anchored_lens)
    run = 0
    has_value_sized_run = False
    for is_cov in covered:
        if is_cov:
            run = 0
        else:
            run += 1
            if run * 2 >= scale:
                has_value_sized_run = True
                break
    return leftover if has_value_sized_run else None


# Issue#209：页级 HaS NER 死线。同一页会多次调用 NER（语义组、文本块组…），
# 每次各自从"现在"起算预算会把总时长拼爆（门④ 实测：单页 280s 的 NER 分若干
# 次调用，没有一次超过预算，页门限 300s 照旧杀页）。用 ContextVar 把死线钉在
# **页的上下文**上：页级入口 reset 一次，之后本页所有 NER 调用共用同一条墙；
# asyncio 每个页任务有独立上下文，并发页之间互不串味。
_PAGE_NER_DEADLINE: ContextVar[float | None] = ContextVar(
    "has_text_page_ner_deadline", default=None
)


def reset_page_ner_deadline() -> None:
    """页级入口调用：清掉上一页留下的死线，让本页重新起算。"""
    _PAGE_NER_DEADLINE.set(None)


def has_text_page_deadline() -> float | None:
    """本页的 HaS NER 死线（首次调用时建立，后续复用）。"""
    deadline = _PAGE_NER_DEADLINE.get()
    if deadline is not None:
        return deadline
    budget = float(getattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 0.0) or 0.0)
    if budget <= 0:
        return None
    deadline = time.monotonic() + budget
    _PAGE_NER_DEADLINE.set(deadline)
    return deadline


class HasTextNerPageBudgetExceeded(RuntimeError):
    """页级 HaS NER 预算耗尽（Issue#209）。

    语气与「页超时」同级：该页**只被扫了一部分**，不得当整页完成看待。由
    HAS_NER_PAGE_BUDGET_STRICT_FAIL 决定是否抛出——抛出后由页级 stage 捕获并
    记入 `recognition.failed_pages`（页级隔离，不连坐整卷）。
    """


async def run_has_text_analysis(
    ocr_blocks: list[OCRTextBlock],
    has_client: Any,
    vision_types: list | None = None,
    stage_status: dict[str, Any] | None = None,
    page_deadline: float | None = None,
) -> list[dict[str, str]]:
    """
    Analyse OCR text with HaS local NER model to identify sensitive entities.
    Fully offline - no cloud API dependency.

    Args:
        ocr_blocks: OCR text blocks.
        has_client: HaSClient instance (may be None).
        vision_types: User-enabled vision type configs.
        page_deadline: 调用方给的**页级**死线（``time.monotonic()`` 口径）。页内会
            多次调用本函数（语义组、文本块组…），各自从"现在"起算预算会把这些
            调用拼成远超页门限的总时长——门④ 实测 2 页夹具里页 133 正是这样跑满
            300s 而预算一次没触发。传了死线就以它为准（预算秒数仍用于记账文案）。

    Returns:
        [{type: "PERSON", text: "张三"}, ...]
    """
    total_start = time.perf_counter()
    # Issue#209：页级 NER 预算。真实案卷 133 页实测「主查枚举复读 83.4s + 9~12 段
    # 补查 + 外层整页重发」在 NER 上烧掉 >420s，撞 300s 页墙后整页清零。给 NER
    # 一条自己的墙：超预算后停发剩余阶段，用已聚合的部分结果收尾并显式记账；
    # strict=True 时把「只扫了一部分」落成页级失败（语气与页超时一致：进
    # failed_pages ⇒ 交付面拦截 + 界面可查），而不是伪装成完成的整页结果。
    page_budget_sec = float(getattr(settings, "HAS_NER_PAGE_BUDGET_SEC", 0.0) or 0.0)
    if page_budget_sec > 0:
        own_deadline: float | None = time.monotonic() + page_budget_sec
    else:
        own_deadline = None
    # 调用方（页级 stage）给的死线优先：同一页的多次 NER 调用共用一条墙。
    page_deadline = page_deadline if page_deadline is not None else own_deadline
    budget_strict = bool(getattr(settings, "HAS_NER_PAGE_BUDGET_STRICT_FAIL", True))

    def _budget_exhausted() -> bool:
        return page_deadline is not None and time.monotonic() >= page_deadline

    def _note_budget_exceeded(reason: str) -> None:
        """记预算突破 + 记账；strict 时落成页级失败（Issue#209）。"""
        _record_has_text_metric(stage_status, "has_text_ner_budget_exceeded", 1)
        elapsed_ms = round((time.perf_counter() - total_start) * 1000)
        _record_has_text_metric(stage_status, "has_text_ner_budget_elapsed_ms", elapsed_ms)
        logger.warning(
            "HaS page NER budget exceeded after %.1fs (budget=%.1fs): %s",
            elapsed_ms / 1000,
            page_budget_sec,
            reason,
        )
        if budget_strict:
            raise HasTextNerPageBudgetExceeded(
                f"HaS NER budget exceeded after {elapsed_ms / 1000:.1f}s "
                f"(budget {page_budget_sec:.0f}s): {reason}; page scanned only partially"
            )

    _record_has_text_metric(stage_status, "has_text_cache_status", "not_started")
    _record_has_text_metric(stage_status, "has_text_slot_wait_ms", 0)
    _record_has_text_metric(stage_status, "has_text_duplicate_wait_ms", 0)
    _record_has_text_metric(stage_status, "has_text_model_ms", 0)

    if not ocr_blocks:
        _record_has_text_metric(stage_status, "has_text_cache_status", "skipped_empty_ocr")
        _record_has_text_metric(
            stage_status,
            "has_text_total_ms",
            round((time.perf_counter() - total_start) * 1000),
        )
        return []


    # Lazy re-init if client was not available at startup
    if not has_client:
        try:
            from app.services.has_client import HaSClient
            has_client = HaSClient()
        except Exception as e:
            logger.error("HaS Client init failed: %s", e)
            _record_has_text_metric(stage_status, "has_text_cache_status", "skipped_no_client")
            _record_has_text_metric(
                stage_status,
                "has_text_total_ms",
                round((time.perf_counter() - total_start) * 1000),
            )
            return []

    if _has_recent_negative_health(has_client):
        logger.warning("HaS service recently reported unavailable, skipping NER")
        _record_has_text_metric(stage_status, "has_text_cache_status", "skipped_recent_unavailable")
        _record_has_text_metric(
            stage_status,
            "has_text_total_ms",
            round((time.perf_counter() - total_start) * 1000),
        )
        return []

    try:
        prepare_start = time.perf_counter()
        selected_type_ids = [_canonical_image_text_type(getattr(vt, "id", "")) for vt in (vision_types or [])]
        candidate_blocks = _filter_blocks_for_has_text(ocr_blocks, selected_type_ids)
        _record_has_text_metric(stage_status, "has_text_reconstructed_lines", 0)
        has_payload = _build_has_text_payload(
            candidate_blocks,
            max_chars=settings.HAS_VISION_MAX_TEXT_CHARS,
            max_block_chars=settings.HAS_VISION_MAX_BLOCK_CHARS,
        )
        text_content = has_payload.content
        _add_has_text_duration(
            stage_status,
            "has_text_prepare_ms",
            round((time.perf_counter() - prepare_start) * 1000),
        )
        _record_has_text_metric(stage_status, "has_text_source_blocks", has_payload.source_block_count)
        _record_has_text_metric(stage_status, "has_text_eligible_blocks", has_payload.eligible_block_count)
        _record_has_text_metric(stage_status, "has_text_unique_blocks", len(has_payload.texts))
        _record_has_text_metric(stage_status, "has_text_duplicate_blocks", has_payload.duplicate_block_count)
        _record_has_text_metric(stage_status, "has_text_clipped_blocks", has_payload.clipped_block_count)
        _record_has_text_metric(stage_status, "has_text_input_chars", has_payload.input_chars)
        _record_has_text_metric(stage_status, "has_text_emitted_chars", has_payload.emitted_chars)
        _record_has_text_metric(stage_status, "has_text_omitted_chars", has_payload.omitted_chars)
        _record_has_text_metric(stage_status, "has_text_truncated", has_payload.truncated)

        if not text_content.strip():
            logger.info(
                "HaS skipped; no eligible OCR text blocks (source=%d, eligible=%d, duplicates=%d)",
                has_payload.source_block_count,
                has_payload.eligible_block_count,
                has_payload.duplicate_block_count,
            )
            _record_has_text_metric(stage_status, "has_text_cache_status", "skipped_no_eligible_text")
            _record_has_text_metric(
                stage_status,
                "has_text_total_ms",
                round((time.perf_counter() - total_start) * 1000),
            )
            return []

        min_text_chars = int(settings.HAS_VISION_MIN_TEXT_CHARS_FOR_NER)
        compact_chars = len(_compact_text(text_content))
        _record_has_text_metric(stage_status, "has_text_compact_chars", compact_chars)
        if compact_chars < min_text_chars:
            logger.info(
                "HaS skipped; compact OCR text chars=%d below min=%d (eligible=%d)",
                compact_chars,
                min_text_chars,
                has_payload.eligible_block_count,
            )
            _record_has_text_metric(stage_status, "has_text_cache_status", "skipped_too_short")
            _record_has_text_metric(
                stage_status,
                "has_text_total_ms",
                round((time.perf_counter() - total_start) * 1000),
            )
            return []

        logger.info(
            (
                "HaS analyzing unique_blocks=%d/%d, source_blocks=%d, "
                "input_chars=%d, emitted_chars=%d, duplicate_blocks=%d, "
                "clipped_blocks=%d, omitted_chars=%d, type_configs=%d, truncated=%s"
            ),
            len(has_payload.texts),
            has_payload.eligible_block_count,
            has_payload.source_block_count,
            has_payload.input_chars,
            has_payload.emitted_chars,
            has_payload.duplicate_block_count,
            has_payload.clipped_block_count,
            has_payload.omitted_chars,
            len(vision_types or []),
            has_payload.truncated,
        )

        # ----- type ID <-> Chinese name mappings -----

        if vision_types:
            chinese_types = _build_has_text_type_names(vision_types)
            if not chinese_types:
                logger.info("HaS skipped; selected OCR types are visual-only")
                _record_has_text_metric(stage_status, "has_text_cache_status", "skipped_visual_only_types")
                _record_has_text_metric(
                    stage_status,
                    "has_text_total_ms",
                    round((time.perf_counter() - total_start) * 1000),
                )
                return []
            logger.info("HaS using types for NER: %s", chinese_types)
        else:
            chinese_types = _build_has_text_type_names()
            logger.info("HaS using default types: %s", chinese_types)
        _record_has_text_metric(stage_status, "has_text_type_count", len(chinese_types))

        # 生效上下文（C1）：主查分支里取；residual 在缓存命中路径惰性取。
        context_tokens: int | None = None
        ner_result = _get_cached_has_text_ner(has_client, text_content, chinese_types)
        if ner_result is not None:
            _record_has_text_metric(stage_status, "has_text_cache_status", "hit_before_slot")
            logger.info("HaS NER cache hit before local slot wait")
        else:
            _record_has_text_metric(stage_status, "has_text_cache_status", "miss")
            # Issue#104 C1/C3：生效上下文取服务端自报值（探测失败回落安全小值），
            # 整页文本按 token 预算分成若干次主查。单组时组文本 == 整页文本，
            # 与现状逐字等价（C6：同一 payload、同一闸位数、同一 K 趟采样）。
            context_tokens = await _resolve_effective_context(has_client)
            try:
                group_texts = plan_has_text_group_texts(
                    has_payload.texts, chinese_types, context_tokens=context_tokens
                )
            except ValueError as exc:
                # D1⑩：上下文小到连一个字符都放不下时规划器**显式**报错。这里
                # 必须把它落成「该页判失败 + 专属状态」，否则只会看到通用兜底的
                # has_text_cache_status=failed，运维无法判断是预算口径配错。
                _record_has_text_metric(stage_status, "has_text_cache_status", "budget_error")
                logger.error("HaS text NER page judged FAILED (context budget): %s", exc)
                return []
            _record_has_text_metric(stage_status, "has_text_payload_group_count", len(group_texts))
            logger.info(
                "HaS payload planned into %d group(s) (context=%d tokens, emitted_chars=%d)",
                len(group_texts),
                context_tokens,
                has_payload.emitted_chars,
            )
            ner_result, groups_failed = await _run_has_text_ner_groups(
                has_client,
                stage_status,
                group_texts,
                chinese_types,
                context_tokens=context_tokens,
                deadline=page_deadline,
            )
            if (stage_status or {}).get("has_text_ner_budget_exceeded"):
                # Issue#209：主查阶段已有组被预算跳过 ⇒ 该页只被扫了一部分。
                # strict（默认）⇒ 落成页级失败；否则用部分结果收尾并留痕。
                _note_budget_exceeded(
                    "main NER phase stopped early: "
                    f"{(stage_status or {}).get('has_text_ner_budget_skipped_groups', 0)}"
                    f"/{len(group_texts)} group(s) skipped"
                )
            if groups_failed:
                # Issue#104 C5：任一组失败 ⇒ 该页主查整体判失败——组级部分结果
                # 不得伪装成「该页无实体」静默通过（那正是本 Issue 在修的病）。
                _record_has_text_metric(stage_status, "has_text_ner_failed_groups", groups_failed)
                _record_has_text_metric(stage_status, "has_text_cache_status", "group_failed")
                logger.error(
                    "HaS text NER page judged FAILED: %d/%d group(s) failed; not merging partial results",
                    groups_failed,
                    len(group_texts),
                )
                _record_has_text_metric(
                    stage_status,
                    "has_text_total_ms",
                    round((time.perf_counter() - total_start) * 1000),
                )
                return []

        if not ner_result or not isinstance(ner_result, dict):
            logger.info("HaS: no entities found by NER")
            _record_has_text_metric(stage_status, "has_text_entity_count", 0)
            _record_has_text_metric(
                stage_status,
                "has_text_total_ms",
                round((time.perf_counter() - total_start) * 1000),
            )
            return []

        logger.info("HaS NER result: %s", ner_result)

        # ----- reverse mapping: Chinese -> type ID -----
        # Every label the prompt asked for on an item's behalf maps back to
        # that item (the request's own labels are the same source the prompt was
        # built from, so query and answer stay symmetric — 大写金额 -> AMOUNT).
        if vision_types:
            # Tag-by-request (same principle as the LA chain): every result
            # bucket key is a label WE sent for a checked item, so the map is
            # built purely from the request — item name, item id, and the
            # item's own query labels. No registry lookups: the checklist owns
            # the vocabulary end to end.
            chinese_to_id = {}
            for vt in vision_types:
                normalized_id = _canonical_image_text_type(vt.id)
                if not normalized_id:
                    continue
                chinese_to_id[vt.name] = normalized_id
                chinese_to_id[normalized_id] = normalized_id
                for query_label in _item_query_labels(vt):
                    chinese_to_id[query_label] = normalized_id
        else:
            chinese_to_id = {}
            for item in _default_has_text_items():
                normalized_id = _canonical_image_text_type(item.id)
                if not normalized_id:
                    continue
                chinese_to_id[item.name] = normalized_id
                chinese_to_id[normalized_id] = normalized_id
                for query_label in _item_query_labels(item):
                    chinese_to_id[query_label] = normalized_id

        bridge_ner_result: dict[str, list[str]] = {}
        bridge_blocks = reconstruct_visual_line_blocks(candidate_blocks)
        _record_has_text_metric(stage_status, "has_text_reconstructed_lines", len(bridge_blocks))
        if bridge_blocks:
            bridge_payload = _build_has_text_payload(
                bridge_blocks,
                max_chars=min(settings.HAS_VISION_MAX_TEXT_CHARS, _BRIDGE_PAYLOAD_MAX_CHARS),
                max_block_chars=settings.HAS_VISION_MAX_BLOCK_CHARS,
            )
            bridge_text = bridge_payload.content
            if bridge_text.strip():
                cached_bridge = _get_cached_has_text_ner(has_client, bridge_text, chinese_types)
                if cached_bridge is not None:
                    bridge_ner_result = cached_bridge
                else:
                    from app.core.gpu_inference_gate import shared_gpu_inference_slot

                    async with shared_gpu_inference_slot("OCR HaS Text bridge NER"):
                        cached_bridge = _get_cached_has_text_ner(has_client, bridge_text, chinese_types)
                        if cached_bridge is not None:
                            bridge_ner_result = cached_bridge
                        else:
                            model_start = time.perf_counter()
                            result = await asyncio.to_thread(has_client.ner, bridge_text, chinese_types)
                            _add_has_text_duration(
                                stage_status,
                                "has_text_model_ms",
                                round((time.perf_counter() - model_start) * 1000),
                            )
                            bridge_ner_result = result if isinstance(result, dict) else {}

        entities = []
        min_len_by_type = _NER_MIN_LEN_BY_TYPE

        merged_ner_result = dict(ner_result)
        for entity_type, entity_list in bridge_ner_result.items():
            if not entity_list:
                continue
            merged_ner_result.setdefault(entity_type, [])
            for text in entity_list:
                clean_text = _compact_text(text)
                if clean_text and clean_text not in merged_ner_result[entity_type]:
                    merged_ner_result[entity_type].append(clean_text)

        # ---- Residual re-ask pass (0712 海关发票实证) ----
        # 长 payload 召回稀释: 161块×38标签一次NER, 金额桶16值恰好漏掉
        # USD 4,700.00/USD 125.00(同标签块级14/14全召回; temp=0确定性; 收窄/
        # 匹配无辜)。"已消费"判据=模型自己的答案, 但用 _block_residual_ink 的
        # token 级字符覆盖(isolated-token 扣除, 不再 bare-containment): 返回值
        # 只有作为整词出现才算解释该块的那段墨迹, 扣完仍剩 >= 本块最短锚定值
        # 一半长度的连续墨迹=还有第二列的值未解释 → 未消费; 只剩字段标签碎片
        # =已解释。未消费块只把"未解释的那段墨迹"拼残差 payload(丢掉已消费
        # 兄弟值与标签)再问一次——短上下文召回完美(实测), 且残差 != 整页文本,
        # 单块合并双列也能过 residual_text != text_content 门控。
        # 附加式合并只增不减: force-fit 最坏=表头多遮一块; 返回空=与现状全等。
        consumed_values: list[str] = []
        for entity_type, entity_list in merged_ner_result.items():
            normalized_type = chinese_to_id.get(entity_type, entity_type)
            value_min_len = _NER_MIN_LEN_BY_TYPE.get(normalized_type, _NER_DEFAULT_MIN_LEN)
            for value in entity_list:
                compact_value = _compact_text(value)
                if compact_value and len(compact_value) >= value_min_len:
                    consumed_values.append(compact_value)
        residual_blocks = []
        for block in candidate_blocks:
            block_compact = _compact_text(_block_search_text(block))
            if not block_compact:
                continue
            residual_ink = _block_residual_ink(block_compact, consumed_values)
            if residual_ink is None:
                continue
            # Re-ask only the unexplained ink, not the whole block: the consumed
            # sibling values are already merged, so dropping them keeps recall
            # while making the residual payload differ from the full page even
            # for a lone merged two-column block (clears the != text_content gate).
            residual_blocks.append(
                OCRTextBlock(
                    text=residual_ink,
                    polygon=list(getattr(block, "polygon", None) or [[0, 0], [0, 0], [0, 0], [0, 0]]),
                    confidence=float(getattr(block, "confidence", 1.0) or 1.0),
                )
            )
        if residual_blocks:
            residual_payload = _build_has_text_payload(
                residual_blocks,
                max_chars=settings.HAS_VISION_MAX_TEXT_CHARS,
                max_block_chars=settings.HAS_VISION_MAX_BLOCK_CHARS,
            )
            residual_text = residual_payload.content
            if residual_text.strip() and residual_text != text_content:
                residual_ner_result = _get_cached_has_text_ner(has_client, residual_text, chinese_types)
                if residual_ner_result is None:
                    from app.core.gpu_inference_gate import shared_gpu_inference_slot

                    # Issue#104 C4：residual 接缝与主查同一条规划/派发路径——
                    # 它同样是 32000 字符上限、同样会超上下文 400（bridge 的
                    # 1200 上限安全，不分组）。组失败按组吞掉（auxiliary pass，
                    # 维持「residual 缺席 = 与旧版等价」的语义），组结果 union。
                    residual_context = (
                        context_tokens
                        if context_tokens is not None
                        else await _resolve_effective_context(has_client)
                    )
                    residual_group_texts = plan_has_text_group_texts(
                        residual_payload.texts,
                        chinese_types,
                        context_tokens=residual_context,
                    )
                    _record_has_text_metric(
                        stage_status, "has_text_residual_group_count", len(residual_group_texts)
                    )
                    residual_group_results: list[Any] = []
                    for residual_group_text in residual_group_texts:
                        if _budget_exhausted():
                            # Issue#209：预算耗尽即停发残差组（strict ⇒ 页失败）。
                            _note_budget_exceeded("residual NER phase stopped early")
                            break
                        cached_group = _get_cached_has_text_ner(has_client, residual_group_text, chinese_types)
                        if cached_group is not None:
                            residual_group_results.append(cached_group)
                            continue
                        async with shared_gpu_inference_slot("OCR HaS Text residual NER"):
                            cached_group = _get_cached_has_text_ner(has_client, residual_group_text, chinese_types)
                            if cached_group is not None:
                                residual_group_results.append(cached_group)
                                continue
                            model_start = time.perf_counter()
                            result = await asyncio.to_thread(
                                has_client.ner,
                                residual_group_text,
                                chinese_types,
                                context_tokens=residual_context,
                            )
                            _add_has_text_duration(
                                stage_status,
                                "has_text_model_ms",
                                round((time.perf_counter() - model_start) * 1000),
                            )
                            residual_group_results.append(result if isinstance(result, dict) else {})
                    residual_ner_result = merge_group_ner_results(residual_group_results)
                # Model-centric verification of the residual re-ask (fixes 甲方→开户行):
                # the residual NER ran on REDUCED text where a label's real value was
                # already consumed, so the open-vocab model can force-fit a wrong span —
                # it read 甲方 as 开户行 because the account sentence around it was left
                # behind and the real 农行… was gone. Confirm each NEW value by re-asking
                # the model about THAT VALUE ON ITS OWN under its label: a real value the
                # long-payload dilution hid (an id number, a 金额) is self-evident and
                # re-confirms alone; a context-only force-fit (甲方, stripped of the
                # account sentence, is plainly not a bank) returns nothing. Context-bound
                # values (a real 开户行/机构名) are the main pass's job via their field
                # label, not the residual's. The model is the judge — no rule, no regex,
                # no threshold.
                residual_new: dict[str, list[str]] = {}
                for entity_type, entity_list in (residual_ner_result or {}).items():
                    existing = set(merged_ner_result.get(entity_type, []))
                    for text in entity_list or []:
                        clean_text = _compact_text(text)
                        if clean_text and clean_text not in existing:
                            bucket = residual_new.setdefault(entity_type, [])
                            if clean_text not in bucket:
                                bucket.append(clean_text)
                if residual_new and _budget_exhausted():
                    # Issue#209：预算耗尽 ⇒ 不再进入逐值自证（strict ⇒ 页级失败）。
                    # 非 strict：未自证的值按「自证未确认」同一保守语义**不采纳**
                    # （不新增假阳性），只用主查+残差已聚合的部分结果收尾。
                    _note_budget_exceeded("residual verify phase skipped")
                    residual_new = {}
                if residual_new:
                    from app.core.config import settings as verify_settings
                    from app.core.gpu_inference_gate import shared_gpu_inference_slot

                    verify_jobs: list[tuple[str, str]] = [
                        (entity_type, clean_text)
                        for entity_type, fresh in residual_new.items()
                        for clean_text in fresh
                    ]
                    # Issue#136：页内自证任务条数硬上限（0 = 不限）。病态密页
                    # 主查收编数百数字串后逐值自证会把单页模型调用推到 500+
                    # （云实测 519 次 / 211.5s）。
                    #
                    # 截断顺序必须**只由输入决定**：residual_new 的类型序来自
                    # 模型返回的 JSON key 顺序（换模型/模板即变），若按它取前 N
                    # 条，「哪些值被丢掉」会随模型版本漂移、档位校准静默失效。
                    # 故按「值在原文中首次出现的位置」升序取前 N；原文里找不到的
                    # 值排最后（这类值本就是模型在缩减文本上的拟合产物，不送自证
                    # 等于不采纳，与「自证未确认」同语义，风险最低）。
                    page_compact = _compact_text(text_content or "")

                    def _verify_order(job: tuple[str, str]) -> tuple[int, int, str, str]:
                        entity_type, clean_text = job
                        position = page_compact.find(clean_text)
                        return (
                            0 if position >= 0 else 1,
                            position if position >= 0 else 0,
                            entity_type,
                            clean_text,
                        )

                    verify_jobs.sort(key=_verify_order)
                    # 被截断的值不进入结果——与「自证未确认」同一语义（保守、
                    # 不新增假阳性），代价是可能少召回，故默认值须由真实案卷
                    # A/B 证据定档（issue136-verify-cap-plan §3 AC3）。
                    verify_jobs_before = len(verify_jobs)
                    verify_skipped = 0
                    max_jobs = max(0, int(verify_settings.HAS_NER_VERIFY_MAX_JOBS_PER_PAGE))
                    if max_jobs and len(verify_jobs) > max_jobs:
                        verify_skipped = len(verify_jobs) - max_jobs
                        _skipped_types = Counter(t for t, _ in verify_jobs[max_jobs:])
                        logger.warning(
                            "[has_text] residual verify capped: %d job(s) -> %d "
                            "(skipped %d, by type: %s); 截断按「值在原文首次出现的位置」序，"
                            "被截断值不再自证（与「自证未确认」同语义）",
                            verify_jobs_before,
                            max_jobs,
                            verify_skipped,
                            dict(sorted(_skipped_types.items())),
                        )
                        verify_jobs = verify_jobs[:max_jobs]
                    # 待自证**值条数**（截断后）。与 verify_state["calls"] 区分：
                    # 批量自证启用时一次调用覆盖多个值 ⇒ calls < jobs。
                    verify_jobs_planned = len(verify_jobs)
                    verify_jobs_by_type = dict(sorted(Counter(t for t, _ in verify_jobs).items()))
                    # 独立于闸门的硬界：闸门旁路（SERIALIZE_SHARED_GPU_MODELS=False
                    # 时 slot 变空操作）下防止 N 个值同时砸向模型服务。
                    window = max(
                        1,
                        min(
                            int(verify_settings.HAS_NER_VERIFY_PARALLEL_CAP),
                            int(verify_settings.HAS_NER_GLOBAL_MAX_INFLIGHT),
                            len(verify_jobs),
                        ),
                    )
                    verify_sem = asyncio.Semaphore(window)
                    verify_state = {
                        "calls": 0,
                        "current": 0,
                        "peak": 0,
                        "errors": 0,
                        "model_ms": 0,
                    }

                    async def _verify_one(job: tuple[str, str]) -> tuple[str, str, bool]:
                        entity_type, clean_text = job
                        async with verify_sem, shared_gpu_inference_slot("OCR HaS Text residual verify NER"):
                            verify_state["calls"] += 1
                            verify_state["current"] += 1
                            verify_state["peak"] = max(verify_state["peak"], verify_state["current"])
                            try:
                                verify = await asyncio.to_thread(has_client.ner, clean_text, [entity_type])
                            except Exception:
                                # 未确认丢弃：与真客户端吞异常返回 {} 的生产语义对齐
                                # （评审 B2）。等价于 gather(return_exceptions=True) 后
                                # 逐项兜底——其余任务照常落定，无孤儿持槽。
                                verify_state["errors"] += 1
                                return entity_type, clean_text, False
                            finally:
                                verify_state["current"] -= 1
                        confirmed = {
                            _compact_text(v)
                            for v in ((verify or {}).get(entity_type, []) or [])
                        }
                        return entity_type, clean_text, clean_text in confirmed

                    batch_size = max(0, int(verify_settings.HAS_NER_VERIFY_BATCH_SIZE))
                    if batch_size > 0:
                        # 批量自证（第二级杠杆，默认关）。同类型待验证值按 N 个一批
                        # 合并质询，确认集 = 模型返回值 ∩ 批内值——逐值语义仍由模型
                        # 回答唯一决定。长列表召回稀释正是 residual 机制诞生的原因
                        # （0712 实证），开启前必须在真实案卷页 A/B 实证。
                        from collections import defaultdict

                        by_type: dict[str, list[str]] = defaultdict(list)
                        for entity_type, clean_text in verify_jobs:
                            by_type[entity_type].append(clean_text)
                        batch_jobs: list[tuple[str, list[str]]] = []
                        for entity_type, values in by_type.items():
                            for i in range(0, len(values), batch_size):
                                batch_jobs.append((entity_type, values[i:i + batch_size]))

                        async def _verify_batch(entity_type: str, values: list[str]) -> list[tuple[str, str, bool]]:
                            async with verify_sem, shared_gpu_inference_slot("OCR HaS Text residual verify NER"):
                                verify_state["calls"] += 1
                                verify_state["current"] += 1
                                verify_state["peak"] = max(verify_state["peak"], verify_state["current"])
                                try:
                                    verify = await asyncio.to_thread(
                                        has_client.ner, "\n".join(values), [entity_type]
                                    )
                                except Exception:
                                    verify_state["errors"] += 1
                                    return [(entity_type, v, False) for v in values]
                                finally:
                                    verify_state["current"] -= 1
                            confirmed_batch = {
                                _compact_text(v)
                                for v in ((verify or {}).get(entity_type, []) or [])
                            }
                            return [(entity_type, v, v in confirmed_batch) for v in values]

                        _verify_t0 = time.perf_counter()
                        outcomes = await asyncio.gather(
                            *(_verify_batch(job[0], job[1]) for job in batch_jobs)
                        )
                        verify_state["model_ms"] = round(
                            (time.perf_counter() - _verify_t0) * 1000
                        )
                        for batch_outcomes in outcomes:
                            for entity_type, clean_text, confirmed_ok in batch_outcomes:
                                if not confirmed_ok:
                                    continue
                                merged_ner_result.setdefault(entity_type, [])
                                if clean_text not in merged_ner_result[entity_type]:
                                    merged_ner_result[entity_type].append(clean_text)
                    else:
                        _verify_t0 = time.perf_counter()
                        outcomes = await asyncio.gather(*(_verify_one(job) for job in verify_jobs))
                        verify_state["model_ms"] = round(
                            (time.perf_counter() - _verify_t0) * 1000
                        )
                        for entity_type, clean_text, confirmed_ok in outcomes:
                            if not confirmed_ok:
                                continue
                            merged_ner_result.setdefault(entity_type, [])
                            if clean_text not in merged_ner_result[entity_type]:
                                merged_ner_result[entity_type].append(clean_text)

                    _record_has_text_metric(stage_status, "has_text_verify_calls", verify_state["calls"])
                    _record_has_text_metric(stage_status, "has_text_verify_skipped", verify_skipped)
                    # 三个口径显式分开，证据包可复算：
                    #   before == after + skipped（恒等式）
                    #   after ≤ N（上限生效）
                    #   calls == after（B=0 逐值） / calls == Σ_t ceil(n_t/B)（B>0 按类型批）
                    _record_has_text_metric(stage_status, "has_text_verify_jobs_before", verify_jobs_before)
                    _record_has_text_metric(stage_status, "has_text_verify_jobs_after", verify_jobs_planned)
                    _record_has_text_metric(
                        stage_status, "has_text_verify_jobs_by_type", verify_jobs_by_type
                    )
                    _record_has_text_metric(stage_status, "has_text_verify_peak_concurrency", verify_state["peak"])
                    _record_has_text_metric(stage_status, "has_text_verify_errors", verify_state["errors"])
                    # 自证阶段**模型墙钟**（同路径、不受 OCR 抖动影响）：
                    # Issue#136 的「墙钟下降」半轴由此量测，而非端到端总墙钟。
                    _record_has_text_metric(
                        stage_status, "has_text_verify_ms", verify_state["model_ms"]
                    )

        for entity_type, entity_list in merged_ner_result.items():
            if not entity_list:
                continue

            # Open vocabulary: the type IS what the model returned. If it matches a
            # label we sent for a schema item, use that item's id; otherwise keep the
            # raw model label (识别出来是啥就是啥) — never drop, never reconcile.
            normalized_type = chinese_to_id.get(entity_type, entity_type)
            min_len = min_len_by_type.get(normalized_type, _NER_DEFAULT_MIN_LEN)

            for entity_text in entity_list:
                text = entity_text.strip() if entity_text else ""
                if normalized_type in {"COMPANY_NAME", "BANK_NAME", "BANK_ACCOUNT", "AMOUNT"}:
                    text = _compact_text(text)
                if not text:
                    continue
                if len(text) < min_len:
                    # Below-min-length values (e.g. 性别 男) are kept: the
                    # matcher attaches them only by block equality or isolated
                    # token (_is_strict_match_entity), never bare containment.
                    logger.debug("HaS kept short value for strict matching: '%s' (%s)", text, normalized_type)

                entities.append({
                    "type": normalized_type,
                    "text": text,
                })
                logger.debug("HaS found entity: %s (%s)", text, normalized_type)

        await _narrow_amount_entities(entities, has_client)

        # Boxes come from matching these values back to OCR blocks; mIoU is the
        # only merge step.
        logger.info("HaS total %d sensitive entities found", len(entities))
        _record_has_text_metric(stage_status, "has_text_entity_count", len(entities))
        _record_has_text_metric(
            stage_status,
            "has_text_total_ms",
            round((time.perf_counter() - total_start) * 1000),
        )
        return entities

    except HasTextNerPageBudgetExceeded:
        # Issue#209：预算突破必须冒到页级 stage ⇒ 记入 recognition.failed_pages
        # （与页超时同一条交付拦截链）。若被下面的宽 except 吞成 []，"只扫了一
        # 部分"就被洗成"该页无实体"——正是本件要防的静默半结果。
        _record_has_text_metric(stage_status, "has_text_cache_status", "page_budget_exceeded")
        _record_has_text_metric(
            stage_status,
            "has_text_total_ms",
            round((time.perf_counter() - total_start) * 1000),
        )
        raise
    except Exception as e:
        logger.exception("HaS text analysis failed: %s", e)
        _record_has_text_metric(stage_status, "has_text_cache_status", "failed")
        _record_has_text_metric(
            stage_status,
            "has_text_total_ms",
            round((time.perf_counter() - total_start) * 1000),
        )
        # NER failed; structural table-amount / form-field recalls are still valid.
        return []
