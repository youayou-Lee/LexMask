"""按任务类型拆分的执行流水线（识别 / 结构化 / 匿名化），以 mixin 形式提供。

从 task_queue.py 拆出。三个 mixin 只通过 self 调用核心队列提供的共享方法
（_get_store / _record_task_started / _record_item_performance /
_try_update_job_status / _refresh_job_status / enqueue），不直接触碰队列
内部数据结构，故对同一 SimpleTaskQueue 实例的状态与引用完全透明。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from app.services.file_parser import PdfEncryptedError
from app.services.task_queue_metrics import (
    _duration_breakdown_from_quality,
    _effective_vision_page_concurrency,
    _elapsed_ms,
    _file_type_value,
    _gpu_memory_metadata,
    _page_vision_quality_from_file_info,
    _page_vision_quality_from_result,
    _utc_iso,
    _vision_page_concurrency_reason,
)

if TYPE_CHECKING:
    from app.services.job_store import JobStore
    from app.services.task_queue import TaskItem

logger = logging.getLogger(__name__)
# Truncation length for stored error messages (name for repeated literal).
_ERROR_MSG_MAX_LEN = 500


def _recognition_failure_state(store: Any, item_id: str) -> tuple[dict[str, str], int]:
    """读条目 performance 的 (页级失败清单, 逐页账目页数)（Issue#199）。

    逐页账目页数用于区分「部分页失败」与「整卷全失败」的摘要措辞（门⑥ 二段 F9）。
    """
    try:
        item = store.get_item(item_id) or {}
        performance = json.loads(item.get("performance_json") or "{}")
        recognition = performance.get("recognition") or {}
        pages = recognition.get("failed_pages") or {}
        failed = {str(k): str(v) for k, v in pages.items()} if isinstance(pages, dict) else {}
        seen = recognition.get("pages")
        return failed, len(seen) if isinstance(seen, dict) else 0
    except (ValueError, TypeError, AttributeError):
        return {}, 0


def _recognition_failed_pages(store: Any, item_id: str) -> dict[str, str]:
    """读条目 performance_json 里的 `recognition.failed_pages`（Issue#199）。

    供 skip_review 守卫等下游消费；缺失/坏 JSON 一律按「无失败页」处理——
    守卫只做加严（多停一次审阅无害），不做加严失败（别把好条目打成异常）。
    """
    return _recognition_failure_state(store, item_id)[0]


def _failed_pages_error_summary(failed_pages: dict[str, str], total_pages: int = 0) -> str | None:
    """页级失败的可读摘要：空清单返回 None（显式清空，不携带陈旧摘要）。

    门⑥ 二段 F7：展示面文案只从权威清单（performance.recognition.failed_pages）派生，
    不再无差别沿用既有 error_message——同状态迁移是 no-op，陈旧摘要会被带到干净条目。
    """
    if not failed_pages:
        return None
    ordered = sorted(failed_pages, key=lambda p: (not p.isdigit(), int(p) if p.isdigit() else 0))
    listed = ", ".join(ordered[:20]) + (" …" if len(ordered) > 20 else "")
    scope = f"all {total_pages} page(s)" if total_pages and len(ordered) >= total_pages else f"{len(ordered)} page(s)"
    return f"{scope} failed recognition (pages {listed}); not scanned for sensitive data"


def _entity_page(entity: Any) -> int:
    try:
        return int((entity or {}).get("page") or 1)
    except (AttributeError, TypeError, ValueError):
        return 1


def _entity_source(entity: Any) -> str:
    return str((entity or {}).get("source") or "")


def _box_source(box: Any) -> str:
    return str((box or {}).get("source") or "")


def _box_page(box: Any) -> int:
    try:
        return int((box or {}).get("page") or 1)
    except (AttributeError, TypeError, ValueError):
        return 1


def _as_entity_dicts(raw: Any) -> list[dict[str, Any]]:
    """file_store 的 entities 可能是模型对象或 dict；统一成 dict 方便按页过滤。"""
    out: list[dict[str, Any]] = []
    for entity in raw if isinstance(raw, list) else []:
        if isinstance(entity, dict):
            out.append(entity)
        elif hasattr(entity, "model_dump"):
            try:
                dumped = entity.model_dump()
            except Exception:  # noqa: BLE001 - 脏数据跳过即可
                continue
            if isinstance(dumped, dict):
                out.append(dumped)
    return out


def _boxes_by_page(raw: Any) -> dict[int, list[dict[str, Any]]]:
    """把 ``bounding_boxes``（按页 dict 或扁平 list）归一成 page → [box dict]。"""
    out: dict[int, list[dict[str, Any]]] = {}
    items: list[Any] = []
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list):
                items.extend(value)
            elif isinstance(value, dict):
                items.append(value)
    elif isinstance(raw, list):
        items = list(raw)
    for box in items:
        if hasattr(box, "model_dump"):
            box = box.model_dump()
        if not isinstance(box, dict):
            continue
        out.setdefault(_box_page(box), []).append(box)
    return out


class RecognitionPipelineMixin:
    """识别流水线：解析 -> NER/Vision -> 完成识别（可选入队匿名化）。"""

    async def _run_recognition(self, task: TaskItem) -> None:
        from app.services.job_store import JobItemStatus, JobStatus

        started = time.perf_counter()
        store = self._get_store()
        self._record_task_started(task, store)
        job = store.get_job(task.job_id)
        if not job:
            logger.warning("job %s not found, skip", task.job_id[:8])
            return

        item = store.get_item(task.item_id)
        if not item:
            logger.warning("item %s not found, skip", task.item_id[:8])
            return

        # 跳过已完成 / 已取消的（PENDING 和 PROCESSING 允许重试）
        skip_statuses = (
            JobItemStatus.AWAITING_REVIEW.value,
            JobItemStatus.COMPLETED.value,
            JobItemStatus.CANCELLED.value if hasattr(JobItemStatus, "CANCELLED") else "__none__",
        )
        if item["status"] in skip_statuses:
            logger.info("item %s already %s, skip", task.item_id[:8], item["status"])
            return

        # 检查 job 是否已被取消
        if job.get("status") == JobStatus.CANCELLED.value:
            logger.info("job %s cancelled, skip item %s", task.job_id[:8], task.item_id[:8])
            return

        cfg = json.loads(job.get("config_json") or "{}")

        # Issue#257：整卷识别会覆盖所有页，所以在飞页标记必须在这里清掉——否则
        # 一次"页重试中途崩溃 → 启动/看门狗回落整卷重识别"成功后标记仍非空，
        # 单页重试与准入判断都会被这个幽灵标记永远判 busy（完整评审 Critical-1）。
        from app.services.page_retry_service import in_flight_pages

        if in_flight_pages(item):
            self._clear_page_retry_marker(store, task.item_id)

        try:
            # 标记处理中
            store.update_item_status(task.item_id, JobItemStatus.PROCESSING)
            self._try_update_job_status(store, task.job_id, JobStatus.PROCESSING)

            # 1) 供料批量（Issue#196）：自带解析（MinerU/文本直取），不走标准 parse——
            #    解析重复劳动且扫描件会白跑一次 OCR 预析。解析/直取 → NER → 映射草稿
            #    落 file_store，进 AWAITING_REVIEW 等人工确认映射表；无打码段，
            #    skip_item_review 不适用（审阅是产品语义），绝不入队 redaction。
            if str(job.get("job_type") or "") == "agent_batch":
                stage_started = time.perf_counter()
                carrier = await self._run_agent_md(task, cfg)
                self._record_item_performance(
                    store, task.item_id,
                    {
                        "recognition": {
                            "model_ms": _elapsed_ms(stage_started),
                            "mode": "agent_md",
                            "carrier": carrier,
                        }
                    },
                )
                store.update_item_progress(
                    task.item_id, stage="agent_md", current=1, total=1, message="mapping_ready",
                )
                store.update_item_status(task.item_id, JobItemStatus.AWAITING_REVIEW)
                logger.info("[queue] item=%s agent_md mapping_ready, awaiting_review", task.item_id[:8])
                return

            # 2) 解析
            parse_started = time.perf_counter()
            await self._parse_file(task)
            parse_ms = _elapsed_ms(parse_started)
            self._record_item_performance(store, task.item_id, {"recognition": {"parse_ms": parse_ms}})
            logger.info(
                "[queue] item=%s parse elapsed=%.2fs",
                task.item_id[:8],
                parse_ms / 1000,
            )

            # 3) VL-MD 管线(Issue #66/#50):识别+替换+产物一步完成,不走审阅/回写链路
            if str(cfg.get("pipeline_mode") or "") == "vl_md":
                stage_started = time.perf_counter()
                await self._run_vl_md(task, cfg)
                self._record_item_performance(
                    store, task.item_id,
                    {"recognition": {"model_ms": _elapsed_ms(stage_started), "mode": "vl_md"}},
                )
                return

            # 4) NER/Vision
            stage_started = time.perf_counter()
            await self._run_ner_or_vision(task, cfg)
            recognition_stage_ms = _elapsed_ms(stage_started)
            self._record_item_performance(store, task.item_id, {"recognition": {"model_ms": recognition_stage_ms}})
            logger.info(
                "[queue] item=%s recognition stage elapsed=%.2fs",
                task.item_id[:8],
                recognition_stage_ms / 1000,
            )

            # 3) 完成识别
            self._mark_recognition_complete(task, job, store)

        # Issue#139：Python 3.10 上 `asyncio.TimeoutError` 与内建 `TimeoutError` 不是同一个类
        # （3.11+ 才把两者合并），只捕内建类时超时会落到通用分支被记成 failed；故两处都显式列出。
        # 顺序必须在 `(FileNotFoundError, OSError)` 之前——内建 `TimeoutError` 自 Python 3.3 起
        # 就是 `OSError` 的子类（3.11 变的只是把 `asyncio.TimeoutError` 并进来，不是这条亲缘
        # 关系本身），否则超时会被 I/O 分支抢先吞掉、记成「recognition I/O error」。
        # 行尾 noqa 指令（UP041）必须保留（pyproject `target-version = "py311"`）。
        except (TimeoutError, asyncio.TimeoutError) as e:  # noqa: UP041
            err_msg = str(e)[:_ERROR_MSG_MAX_LEN] or "recognition timed out"
            logger.error("[queue] item=%s recognition timeout: %s", task.item_id[:8], err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except (KeyError, ValueError):
                logger.warning("failed to mark item %s as FAILED (item not found or invalid transition)", task.item_id[:8])
        except (FileNotFoundError, OSError) as e:
            err_msg = str(e)[:_ERROR_MSG_MAX_LEN]
            logger.error("[queue] item=%s recognition I/O error: %s", task.item_id[:8], err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except (KeyError, ValueError):
                logger.warning("failed to mark item %s as FAILED (item not found or invalid transition)", task.item_id[:8])
        except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as e:
            err_msg = str(e)[:_ERROR_MSG_MAX_LEN]
            logger.error("[queue] item=%s recognition failed: %s: %s", task.item_id[:8], type(e).__name__, err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except (KeyError, ValueError):
                logger.warning("failed to mark item %s as FAILED (item not found or invalid transition)", task.item_id[:8])
        except PdfEncryptedError:
            # Issue #32：不落任何条目状态，原样上抛给 worker 顶层——那里会写入
            # exc.user_message（友好中文）并标 FAILED。这里若吞掉，顶层 except
            # PdfEncryptedError 永不可达，中文消息只是 str(exc) 的巧合。
            raise
        except Exception as e:
            err_msg = str(e)[:_ERROR_MSG_MAX_LEN]
            logger.exception("[queue] item=%s recognition failed (unexpected): %s", task.item_id[:8], err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except Exception:
                logger.exception("failed to mark item %s as FAILED", task.item_id[:8])
        finally:
            total_ms = _elapsed_ms(started)
            self._record_item_performance(
                store,
                task.item_id,
                {"recognition": {"finished_at": _utc_iso(), "duration_ms": total_ms}},
            )
            logger.info(
                "[queue] item=%s recognition total elapsed=%.2fs",
                task.item_id[:8],
                total_ms / 1000,
            )
            store.touch_job_updated(task.job_id)
            self._refresh_job_status(store, task.job_id)

    async def _parse_file(self, task: TaskItem) -> None:
        """Parse the uploaded file."""
        from app.services.file_operations import parse_file

        logger.info("[queue] item=%s parse", task.item_id[:8])
        await parse_file(task.file_id)

    async def _run_ner(self, task: TaskItem, entity_type_ids: list) -> None:
        """Run text recognition."""
        from app.services.file_operations import hybrid_ner

        store = self._get_store()
        store.update_item_progress(
            task.item_id,
            stage="ner",
            current=1,
            total=1,
            message="text_recognition_running",
        )
        logger.info("[queue] item=%s NER (%d types)", task.item_id[:8], len(entity_type_ids))
        ner_started = time.perf_counter()
        await hybrid_ner(task.file_id, entity_type_ids)
        self._record_item_performance(
            store,
            task.item_id,
            {
                "recognition": {
                    "mode": "text",
                    "ner_ms": _elapsed_ms(ner_started),
                    "entity_type_count": len(entity_type_ids),
                }
            },
        )
        store.update_item_progress(
            task.item_id,
            stage="ner",
            current=1,
            total=1,
            message="text_recognition_complete",
        )

    # ------------------------------------------------------------------
    # Issue#257 单页重试：只跑被点名的失败页，跑完把页账翻新并消账
    # ------------------------------------------------------------------
    async def _run_page_retry(self, task: TaskItem) -> None:
        """重试失败页（能力 2）。

        与整卷识别共用 ``vision_detect``，但语义有三处硬要求：

        * ``merge_existing=True``：保页内既有框（审阅里手工加的框不被冲掉）；
        * ``force=True``：绕开视觉缓存，否则「重试」可能直接拿回旧结果、页永远翻不绿；
        * 页记录与失败清单用 **replace_paths 整体替换**：深合并会把旧的
          ``error``/``stage_duration_ms`` 留在「成功」页上（F1），交付闸会继续读失败账。
        """
        from app.services import file_operations, page_status_service
        from app.services.job_store import JobItemStatus, JobStatus
        from app.services.vision_config import resolve_optional_type_list

        store = self._get_store()
        item = store.get_item(task.item_id)
        job = store.get_job(task.job_id)
        if not item or not job:
            logger.warning("page retry: item/job missing, skip (item=%s)", task.item_id[:8])
            return

        pages = [int(p) for p in (task.meta or {}).get("pages") or []]
        if not pages:
            # 重启补偿（watchdog → repair_stale_processing_items）重建的 TaskItem 只带
            # task_type、不带 meta，页清单必须从 performance 里的标记回落；否则会清标记
            # 后直接返回，item 停在 PENDING 永久卡死（评审 Critical-1）。
            from app.services.page_retry_service import in_flight_pages

            pages = in_flight_pages(item)
        item_status = str(item.get("status") or "")
        job_status = str(job.get("status") or "")
        cancelled = item_status == JobItemStatus.CANCELLED.value or job_status == JobStatus.CANCELLED.value
        if cancelled or not pages:
            self._clear_page_retry_marker(store, task.item_id)
            # submit 是"状态→标记→投递"三段写：崩在标记之前时 item 已是 PROCESSING 却没有
            # 标记，重派回来的任务没有页清单。这里必须把状态落回可继续的态，否则一条
            # 空转任务要等 watchdog（≥max(120s, 2×页超时)）才被整卷重跑（评审 Important-6）。
            if item_status == JobItemStatus.PROCESSING.value:
                try:
                    store.update_item_status(task.item_id, JobItemStatus.AWAITING_REVIEW)
                    store.set_item_error_message(task.item_id, None)
                except Exception:  # noqa: BLE001
                    logger.debug("unable to restore item status after empty page retry", exc_info=True)
            return

        try:
            cfg = json.loads(job.get("config_json") or "{}")
        except (TypeError, ValueError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        ocr_types = resolve_optional_type_list(cfg, "ocr_has_types", "selected_ocr_has_types")
        visual_types = resolve_optional_type_list(
            cfg, "visual_feature_types", "visualFeatureTypes", "selected_visual_feature_types"
        )
        from app.core.config import settings

        page_timeout = float(settings.BATCH_RECOGNITION_PAGE_TIMEOUT)

        recognition = (store.get_item_performance(task.item_id) or {}).get("recognition") or {}
        failed_map: dict[str, str] = {
            str(k): str(v) for k, v in (recognition.get("failed_pages") or {}).items()
        }
        total_pages = int(recognition.get("page_count") or 0)
        if total_pages <= 0:
            # M1：page_count 缺失时不能把总量当 0（错误摘要会误报 "all 0 page(s)"）。
            total_pages = page_status_service.page_count_of(item, file_info=None)

        records: dict[str, dict[str, Any]] = {}
        turned_green: list[int] = []
        for index, page in enumerate(pages, 1):
            started = time.perf_counter()
            started_at = _utc_iso()
            try:
                store.update_item_progress(
                    task.item_id,
                    stage="vision",
                    current=index,
                    total=len(pages),
                    message=f"page retry {index}/{len(pages)}",
                )
            except Exception:
                logger.debug("page retry progress write failed", exc_info=True)
            logger.info(
                "[queue] item=%s page retry %d/%d page=%d START",
                task.item_id[:8], index, len(pages), page,
            )
            try:
                await asyncio.wait_for(
                    file_operations.vision_detect(
                        task.file_id,
                        page,
                        ocr_has_types=ocr_types,
                        visual_feature_types=visual_types,
                        merge_existing=True,
                        force=True,
                    ),
                    timeout=page_timeout,
                )
            except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
                elapsed = _elapsed_ms(started)
                records[str(page)] = {
                    "page": page,
                    "started_at": started_at,
                    "finished_at": _utc_iso(),
                    "duration_ms": elapsed,
                    "status": "timeout",
                }
                failed_map[str(page)] = f"page retry timeout after {page_timeout:.0f}s"
                logger.warning("[queue] item=%s page %d retry TIMEOUT", task.item_id[:8], page)
            except Exception as exc:  # 页级隔离：单页再失败不得作废其它页
                elapsed = _elapsed_ms(started)
                reason = f"{type(exc).__name__}: {exc}"[:_ERROR_MSG_MAX_LEN]
                records[str(page)] = {
                    "page": page,
                    "started_at": started_at,
                    "finished_at": _utc_iso(),
                    "duration_ms": elapsed,
                    "status": "failed",
                    "error": reason,
                }
                failed_map[str(page)] = reason[:200]
                logger.warning(
                    "[queue] item=%s page %d retry FAILED: %s", task.item_id[:8], page, reason
                )
            else:
                elapsed = _elapsed_ms(started)
                records[str(page)] = {
                    "page": page,
                    "started_at": started_at,
                    "finished_at": _utc_iso(),
                    "duration_ms": elapsed,
                    "status": "completed",
                }
                failed_map.pop(str(page), None)
                turned_green.append(page)
                logger.info(
                    "[queue] item=%s page %d retry COMPLETED in %dms",
                    task.item_id[:8], page, elapsed,
                )

        replace_paths: tuple[tuple[str, ...], ...] = (
            ("recognition", "failed_pages"),
            ("recognition", "page_retry_pages"),
        ) + tuple(("recognition", "pages", str(page)) for page in pages)
        try:
            store.update_item_performance(
                task.item_id,
                {
                    "recognition": {
                        "failed_pages": failed_map,
                        "page_retry_pages": [],
                        "pages": records,
                    }
                },
                replace_paths=replace_paths,
            )
        except Exception:
            logger.exception("page retry: failed to persist page ledger for item %s", task.item_id[:8])
            raise

        if turned_green:
            self._merge_retried_boxes_into_review_draft(store, task.item_id, turned_green)

        current = store.get_item(task.item_id) or item
        if str(current.get("status") or "") == JobItemStatus.CANCELLED.value:
            return
        try:
            # 终局进度文案：不写的话行上会一直挂着最后一页的「page retry i/n」陈旧口径。
            if total_pages > 0:
                store.update_item_progress(
                    task.item_id,
                    stage="vision",
                    current=total_pages,
                    total=total_pages,
                    message=(
                        "vision_recognition_complete_with_failures"
                        if failed_map
                        else "vision_recognition_complete"
                    ),
                )
            store.update_item_status(task.item_id, JobItemStatus.AWAITING_REVIEW)
            # 摘要单独写：`update_item_status` 在同状态时跳过 UPDATE（no-op 语义），
            # 而重启补偿后 item 可能已经是 awaiting_review——那时摘要会被静默丢在原地。
            store.set_item_error_message(
                task.item_id, _failed_pages_error_summary(failed_map, total_pages)
            )
            store.touch_job_updated(task.job_id)
            self._refresh_job_status(store, task.job_id)
        except Exception:
            logger.exception("page retry: failed to restore item status for %s", task.item_id[:8])

    def _clear_page_retry_marker(self, store: Any, item_id: str) -> None:
        try:
            store.update_item_performance(
                item_id,
                {"recognition": {"page_retry_pages": []}},
                replace_paths=(("recognition", "page_retry_pages"),),
            )
        except Exception:
            logger.debug("page retry marker clear failed", exc_info=True)

    def _write_failed_pages(
        self, store: Any, item_id: str, failed_pages: dict[int, str]
    ) -> None:
        """整卷识别的失败清单**整体替换**写（清掉上一次的键）。

        深合并语义下"空 dict 不删键"，重跑全成功后旧失败键会永久留着——页格全绿、
        交付闸却锁死、界面没有重试入口（完整评审 Critical-3）。
        """
        self._record_item_performance(
            store,
            item_id,
            {
                "recognition": {
                    "failed_pages": {
                        str(p): reason for p, reason in sorted(failed_pages.items())
                    }
                }
            },
            replace_paths=(("recognition", "failed_pages"),),
        )

    def _merge_retried_boxes_into_review_draft(
        self, store: Any, item_id: str, pages: list[int]
    ) -> None:
        """把重试成功页的新框并进已保存的审阅草稿。

        草稿在交付时会经 ``seed_file_store_from_draft_payload`` **整份**覆盖 file_store 的
        entities 与 bounding_boxes，所以框和实体都必须并进草稿，否则：
          - 框：交付出去的还是「该页无框」的旧草稿；
          - 实体：重试页新认出的 NER 实体被丢掉，交付实体集/映射表缺条目（合规风险，
            评审 Important-3）。
        合并口径：重试页的**自动来源**实体（regex/llm/has）整页换新，人工加的（source=manual）
        保留不动——既不丢用户编辑，也不留陈旧检测结果。
        """
        from app.services.file_management_service import file_store

        try:
            draft = store.get_item_review_draft(item_id)
            if not isinstance(draft, dict):
                return
            draft_boxes = draft.get("bounding_boxes")
            if not isinstance(draft_boxes, list):
                draft_boxes = []
            item = store.get_item(item_id) or {}
            info = file_store.get(str(item.get("file_id") or "")) or {}
            fresh_by_page = _boxes_by_page(info.get("bounding_boxes"))
            wanted = set(pages)
            # 重试页的**自动来源**框整页换新；人工框（source=manual）保留——审阅时手拉的框
            # 是用户的最终判断，重试不该把它抹掉（交付期草稿整份覆盖 file_store，
            # 丢了就是漏打码，评审 Critical-2）。
            kept = [
                box
                for box in draft_boxes
                if not (
                    isinstance(box, dict)
                    and _box_page(box) in wanted
                    and _box_source(box) != "manual"
                )
            ]
            merged = list(kept)
            for page in sorted(wanted):
                merged.extend(fresh_by_page.get(page, []))
            draft_entities = draft.get("entities")
            if isinstance(draft_entities, list):
                fresh_entities = [
                    entity
                    for entity in _as_entity_dicts(info.get("entities"))
                    if _entity_page(entity) in wanted
                ]
                kept_entities = [
                    entity
                    for entity in draft_entities
                    if not (
                        isinstance(entity, dict)
                        and _entity_page(entity) in wanted
                        and _entity_source(entity) != "manual"
                    )
                ]
                seen_ids: set[str] = set()
                entities_merged: list[Any] = []
                for entity in [*kept_entities, *fresh_entities]:
                    key = str((entity or {}).get("id") or "") if isinstance(entity, dict) else ""
                    if key and key in seen_ids:
                        continue
                    if key:
                        seen_ids.add(key)
                    entities_merged.append(entity)
                draft = dict(draft)
                draft["entities"] = entities_merged
            else:
                draft = dict(draft)
            draft["bounding_boxes"] = merged
            store.save_item_review_draft(item_id, draft)
        except Exception:
            logger.exception("page retry: review draft merge failed for item %s", item_id[:8])

    async def _run_vision(self, task: TaskItem, cfg: dict) -> None:
        """Run OCR and visual feature recognition for images or scanned pages."""
        from app.core.config import settings
        from app.services.file_operations import get_file_info, vision_detect
        from app.services.vision_config import resolve_optional_type_list

        fi = get_file_info(task.file_id) or {}
        ocr_types = resolve_optional_type_list(cfg, "ocr_has_types", "selected_ocr_has_types")
        configured_visual_types = resolve_optional_type_list(
            cfg,
            "visual_feature_types",
            "visualFeatureTypes",
            "selected_visual_feature_types",
        )
        visual_feature_types = configured_visual_types
        pages = int(fi.get("page_count") or 1)
        vision_started = time.perf_counter()
        logger.info(
            "[queue] item=%s vision (ocr=%s, visual_features=%s, pages=%d)",
            task.item_id[:8],
            len(ocr_types) if ocr_types is not None else "default",
            len(visual_feature_types) if visual_feature_types is not None else "default",
            pages,
        )
        # Pass empty lists as-is (user explicitly deselected a pipeline).
        # Missing keys stay None so orchestrator defaults still apply.
        page_timeout = float(settings.BATCH_RECOGNITION_PAGE_TIMEOUT)
        configured_page_concurrency = int(settings.BATCH_RECOGNITION_PAGE_CONCURRENCY)
        gpu_memory = None
        if pages > 1 and configured_page_concurrency > 1:
            try:
                from app.core.gpu_memory import query_gpu_memory

                gpu_memory = query_gpu_memory()
            except Exception:
                logger.debug("unable to query GPU memory for vision concurrency", exc_info=True)
        page_concurrency = _effective_vision_page_concurrency(
            fi,
            pages,
            configured_page_concurrency,
            gpu_memory=gpu_memory,
        )
        page_concurrency_reason = _vision_page_concurrency_reason(
            pages,
            configured_page_concurrency,
            page_concurrency,
            gpu_memory,
        )
        sparse_probe: dict[str, Any] = {"ran": False}
        if pages > 1 and _file_type_value(fi.get("file_type")) == "pdf_scanned":
            try:
                from app.services.vision_service import prime_pdf_text_layer_sparse_probe

                sparse_probe = await prime_pdf_text_layer_sparse_probe(
                    str(fi.get("file_path") or ""),
                    fi.get("file_type"),
                    page=1,
                )
            except Exception:
                sparse_probe = {"ran": False, "error": "probe_failed"}
                logger.debug("unable to prime scanned PDF text-layer sparse probe", exc_info=True)
        page_sem = asyncio.Semaphore(page_concurrency)
        store = self._get_store()
        store.update_item_progress(
            task.item_id,
            stage="vision",
            current=0,
            total=pages,
            message=f"Vision recognition queued for {pages} page(s)",
        )
        # Issue#256：视觉要素在队列路径的可用性预检。playground 前端已在提交侧剔除
        # 视觉要素（PR#235 语义：LA 不可用不拦截识别、显式提示不静默丢功能），但
        # API/脚本载荷没有这层保护——此前两段式第二段逐页撞 detect_vision 内的 LA
        # 预检（Issue#217 fail-closed），整卷每页记进 failed_pages（本地无 LA 栈上
        # canary 10 页全失败）。这里在调度前探测一次：LA 不可用 ⇒ 剔除视觉要素
        # （空数组=显式不跑该段）+ 显式落账，OCR+HaS 成果照常交付；LA 在场（云端）
        # 探测即过，行为零变化。预检自身的意外异常不吞（fail-closed 不降级）。
        la_available: bool | None = None  # None=未勾选任何视觉要素（或显式空单），无需探测
        if visual_feature_types is None or len(visual_feature_types) > 0:
            from app.core.errors import AppError
            from app.services.redaction_orchestrator import ensure_visual_service_available

            try:
                await ensure_visual_service_available()
                la_available = True
            except AppError as exc:
                if getattr(exc, "error_code", "") != "visual_service_unavailable":
                    raise
                la_available = False
        if la_available is False:
            visual_feature_types = []
            self._record_item_performance(
                store,
                task.item_id,
                {
                    "recognition": {
                        "visual_detection_skipped": {
                            "at": _utc_iso(),
                            "reason": "la_unavailable",
                            "error_code": "visual_service_unavailable",
                            "message": (
                                "视觉检测服务（LocateAnything）未启动：已跳过视觉识别项"
                                "（印章/签字等），本次仅完成 OCR+HaS 识别；如需视觉识别，"
                                "请启动视觉服务后重新识别。"
                            ),
                        }
                    }
                },
            )
            logger.warning(
                "[queue] item=%s visual detection skipped: LocateAnything unavailable "
                "(visual types stripped, OCR+HaS unaffected)",
                task.item_id[:8],
            )
            store.update_item_progress(
                task.item_id,
                stage="vision",
                current=0,
                total=pages,
                message="visual_detection_skipped_la_unavailable",
            )
        elif la_available is True:
            # 冷审 I1 同款意图（failed_pages 先例）：LA 恢复后重跑必须掉陈旧的
            # visual_detection_skipped，否则条目永远带着「已跳过视觉」的假账。
            # 注意清账写 **None** 而非空 dict——deep-merge 对空 dict 是递归合并
            # （旧键存活，#199 的 failed_pages 清账正因此在生产未生效，另案），
            # None 才会整体覆盖。
            self._record_item_performance(
                store,
                task.item_id,
                {"recognition": {"visual_detection_skipped": None}},
            )
        # Issue#238：段数动态——两段式只在「勾选视觉要素且多页」时存在；单段不编号。
        # current_segment 随段起跑覆写（段号/段数/段名/该段实际并发），前端数字行、
        # 段名行编号与「并行处理中」都从这里取，不靠前端猜配置。
        segment_total = 2 if (pages > 1 and visual_feature_types != []) else 1
        self._record_item_performance(
            store,
            task.item_id,
            {
                "recognition": {
                    "mode": "vision",
                    "page_count": pages,
                    "page_concurrency": page_concurrency,
                    "page_concurrency_configured": configured_page_concurrency,
                    "page_concurrency_reason": page_concurrency_reason,
                    "gpu_memory": _gpu_memory_metadata(gpu_memory),
                    "pdf_text_layer_sparse_probe": sparse_probe,
                    "current_segment": {
                        "index": 1,
                        "total": segment_total,
                        "label": "OCR+HaS识别" if segment_total == 2 else "视觉识别",
                        "concurrency": page_concurrency,
                    },
                    "pages": {},
                }
            },
        )
        active_pages = 0
        max_active_pages = 0
        # Issue#238：进度写点 = 页**终态**（完成/超时/失败都计——页账走到头才算数），
        # 单调防回退；每段归零（两段式各扫一遍，段内 current/total）。
        completed_pages = 0
        reported_current = 0

        def bump_stage_progress(stage_label_: str, stage_total_: int) -> None:
            nonlocal completed_pages, reported_current
            completed_pages += 1
            # 门⑥ M1：现调用形态下每页每段恰 bump 一次、计数严格递增，本闸为死防御——
            # 留作未来补查/重试路径复用 bump 时的防回退护栏（勿删）。
            if completed_pages <= reported_current:
                return
            reported_current = completed_pages
            store.update_item_progress(
                task.item_id,
                stage="vision",
                current=completed_pages,
                total=stage_total_,
                message=f"{stage_label_} page {completed_pages}/{stage_total_}",
            )

        async def run_page(
            p: int,
            *,
            selected_ocr_types: list[str] | None,
            selected_visual_feature_types: list[str] | None,
            merge_existing: bool = False,
            signature_ocr_types: list[str] | None = None,
            signature_visual_feature_types: list[str] | None = None,
            stage_label: str = "视觉识别",
            stage_total: int | None = None,
            stage_failed: dict[int, str] | None = None,
        ) -> None:
            nonlocal active_pages, max_active_pages
            # Issue#238：本段实际要跑的页数（合并段会跳过一阶段失败页，total 缩水才不卡条）。
            stage_pages = pages if stage_total is None else stage_total
            async with page_sem:
                page_started = time.perf_counter()
                page_started_at = _utc_iso()
                active_pages += 1
                max_active_pages = max(max_active_pages, active_pages)
                active_at_start = active_pages
                logger.info(
                    "[queue] item=%s %s page %d/%d START (page_concurrency=%d active_pages=%d)",
                    task.item_id[:8], stage_label, p, stage_pages, page_concurrency, active_at_start,
                )
                self._record_item_performance(
                    store,
                    task.item_id,
                    {
                        "recognition": {
                            "pages": {
                                str(p): {
                                    "page": p,
                                    "started_at": page_started_at,
                                    "active_pages_at_start": active_at_start,
                                    "page_concurrency": page_concurrency,
                                }
                            }
                        }
                    },
                    replace_paths=(("recognition", "pages", str(p)),),
                )
                try:
                    result = await asyncio.wait_for(
                        vision_detect(
                            task.file_id,
                            p,
                            ocr_has_types=selected_ocr_types,
                            visual_feature_types=selected_visual_feature_types,
                            merge_existing=merge_existing,
                            signature_ocr_has_types=signature_ocr_types,
                            signature_visual_feature_types=signature_visual_feature_types,
                        ),
                        timeout=page_timeout,
                    )
                # Issue#139：见 _run_recognition 处的同类说明（3.10 两类分立 + 必须重排到 OSError 之前）。
                except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
                    page_ms = _elapsed_ms(page_started)
                    active_pages = max(0, active_pages - 1)
                    self._record_item_performance(
                        store,
                        task.item_id,
                        {
                            "recognition": {
                                "pages": {
                                    str(p): {
                                        "finished_at": _utc_iso(),
                                        "duration_ms": page_ms,
                                        "status": "timeout",
                                        "active_pages_at_end": active_pages,
                                    }
                                }
                            }
                        },
                    replace_paths=(("recognition", "pages", str(p)),),
                    )
                    # Issue#199 页级隔离：单页超时**不上抛**——记录后由本阶段收尾
                    # 汇入 failed_pages，其余页照常完成（极端页不得作废整卷）。
                    if stage_failed is not None:
                        stage_failed[p] = (
                            f"{stage_label} page timeout after {page_timeout:.0f}s"
                        )
                    # Issue#238：超时页也计入已完成——页账走到头才算数，进度条不卡条。
                    bump_stage_progress(stage_label, stage_pages)
                except MemoryError:
                    # 冷审 I2：OOM 下继续跑其余页只会放大故障——显式上抛走 FAILED 路径。
                    raise
                except Exception as exc:  # noqa: BLE001
                    page_ms = _elapsed_ms(page_started)
                    active_pages = max(0, active_pages - 1)
                    self._record_item_performance(
                        store,
                        task.item_id,
                        {
                            "recognition": {
                                "pages": {
                                    str(p): {
                                        "finished_at": _utc_iso(),
                                        "duration_ms": page_ms,
                                        "status": "failed",
                                        "active_pages_at_end": active_pages,
                                    }
                                }
                            }
                        },
                    replace_paths=(("recognition", "pages", str(p)),),
                    )
                    # Issue#199 页级隔离：同上。CancelledError/KeyboardInterrupt/
                    # SystemExit 是 BaseException，不在此捕获口径内，照常穿透。
                    if stage_failed is not None:
                        stage_failed[p] = f"{stage_label}: {type(exc).__name__}: {exc}"[:200]
                    # 门⑥ 第三段复核 must-fix：失败页此前**不落 pipeline_status**，
                    # 于是「预算/孤儿线程/跳过组数」这些排查面在失败页上全丢
                    # （只有 completed 分支会写）。这里按 **completed 分支同一张表**
                    # （`recognition.pages.<p>.pipeline_status[label]`）补记阶段状态。
                    #
                    # 耗时用**本页实测**的 `page_ms`：不再读
                    # `get_ocr_has_vision_service().last_duration_ms` —— 那是模块级
                    # 单例的「最后一次调用」状态，`BATCH_RECOGNITION_PAGE_CONCURRENCY`
                    # 可 >1，该页若在 ocr_has 之前/之外失败，会把**别的页**的耗时
                    # 记到本页（第三段复核 I4-b 指出的归因风险）。
                    self._record_item_performance(
                        store,
                        task.item_id,
                        {
                            "recognition": {
                                "pages": {
                                    str(p): {
                                        "page": p,
                                        "status": "failed",
                                        "error": f"{type(exc).__name__}: {exc}"[:500],
                                        "stage_duration_ms": {stage_label: page_ms},
                                        "pipeline_status": {
                                            stage_label: {
                                                "failed": True,
                                                "error": f"{type(exc).__name__}: {exc}"[:200],
                                            }
                                        },
                                    }
                                }
                            }
                        },
                    replace_paths=(("recognition", "pages", str(p)),),
                    )
                    # Issue#238：失败页同样计入已完成（失败清单另走 failed_pages 账面）。
                    bump_stage_progress(stage_label, stage_pages)
                else:
                    page_ms = _elapsed_ms(page_started)
                    active_pages = max(0, active_pages - 1)
                    active_at_end = active_pages
                    quality = _page_vision_quality_from_result(result)
                    if not any(quality.values()):
                        quality = _page_vision_quality_from_file_info(get_file_info(task.file_id) or {}, p)
                    self._record_item_performance(
                        store,
                        task.item_id,
                        {
                            "recognition": {
                                "pages": {
                                    str(p): {
                                        "page": p,
                                        "finished_at": _utc_iso(),
                                        "duration_ms": page_ms,
                                        "status": "completed",
                                        "active_pages_at_end": active_at_end,
                                        "duration_breakdown_ms": _duration_breakdown_from_quality(quality),
                                        "cache_status": dict(quality.get("cache_status") or {}),
                                        "pipeline_status": dict(quality.get("pipeline_status") or {}),
                                        "warnings": list(quality.get("warnings") or []),
                                    }
                                }
                            }
                        },
                    replace_paths=(("recognition", "pages", str(p)),),
                    )
                    logger.info(
                        "[queue] item=%s %s page %d/%d DONE elapsed=%.2fs active_pages=%d",
                        task.item_id[:8],
                        stage_label,
                        p,
                        stage_pages,
                        page_ms / 1000,
                        active_at_end,
                    )
                    # Issue#238：完成页即上报——前端「已完成」从此是真值。
                    bump_stage_progress(stage_label, stage_pages)

        async def run_page_stage(
            *,
            selected_ocr_types: list[str] | None,
            selected_visual_feature_types: list[str] | None,
            merge_existing: bool = False,
            signature_ocr_types: list[str] | None = None,
            signature_visual_feature_types: list[str] | None = None,
            stage_label: str = "视觉识别",
            page_filter: set[int] | None = None,
        ) -> dict[int, str]:
            """跑一阶段页任务，返回本阶段失败页 {page: reason}（Issue#199 页级隔离）。

            页级失败在 run_page 内记录并就地返回，不再触发「首错取消全部兄弟页」；
            cancel+raise 处理器只接基础设施异常（store/调度层）。`page_filter` 供
            二阶段（视觉特征合并）跳过一阶段已失败的页。
            """
            stage_failed: dict[int, str] = {}
            page_ids = (
                [p for p in range(1, max(1, pages) + 1) if p in page_filter]
                if page_filter is not None
                else list(range(1, max(1, pages) + 1))
            )
            # Issue#238：段计数归零——两段式各自从 1/total 重扫，单调闸按段重置。
            nonlocal completed_pages, reported_current
            completed_pages = 0
            reported_current = 0
            page_tasks = {
                asyncio.create_task(
                    run_page(
                        p,
                        selected_ocr_types=selected_ocr_types,
                        selected_visual_feature_types=selected_visual_feature_types,
                        merge_existing=merge_existing,
                        signature_ocr_types=signature_ocr_types,
                        signature_visual_feature_types=signature_visual_feature_types,
                        stage_label=stage_label,
                        stage_total=len(page_ids),
                        stage_failed=stage_failed,
                    )
                ): p
                for p in page_ids
            }
            try:
                for page_task in asyncio.as_completed(page_tasks):
                    await page_task
            except Exception:
                for page_task in page_tasks:
                    page_task.cancel()
                raise
            return stage_failed

        try:
            if pages > 1 and visual_feature_types != []:
                # Merge-pass concurrency: default 1 = historical serial behaviour;
                # capped by the effective page concurrency so the GPU-saturation
                # downgrade above also applies here.
                visual_merge_concurrency = max(
                    1,
                    min(
                        int(settings.BATCH_VISUAL_MERGE_PAGE_CONCURRENCY),
                        page_concurrency,
                    ),
                )
                logger.info(
                    "[queue] item=%s vision multi-page scheduling: OCR first (concurrency=%d), then visual features merge pass (concurrency=%d)",
                    task.item_id[:8],
                    page_concurrency,
                    visual_merge_concurrency,
                )
                failed_pages: dict[int, str] = {}
                failed_pages.update(
                    await run_page_stage(
                        selected_ocr_types=ocr_types,
                        selected_visual_feature_types=[],
                        stage_label="OCR+HaS识别",
                    )
                )
                page_sem = asyncio.Semaphore(visual_merge_concurrency)
                # Issue#238：第二段起跑覆写 current_segment——段号/段名/该段实际并发。
                self._record_item_performance(
                    store,
                    task.item_id,
                    {
                        "recognition": {
                            "current_segment": {
                                "index": 2,
                                "total": 2,
                                "label": "视觉特征识别",
                                "concurrency": visual_merge_concurrency,
                            }
                        }
                    },
                )
                # Issue#199：合并阶段跳过一阶段已失败的页——页账保留第一阶段结论，
                # 失败原因带阶段标签（冷审 I3b）。
                failed_pages.update(
                    await run_page_stage(
                        selected_ocr_types=[],
                        selected_visual_feature_types=visual_feature_types,
                        merge_existing=True,
                        signature_ocr_types=ocr_types,
                        signature_visual_feature_types=visual_feature_types,
                        stage_label="视觉特征识别",
                        page_filter=set(range(1, max(1, pages) + 1)) - set(failed_pages),
                    )
                )
            else:
                failed_pages = await run_page_stage(
                    selected_ocr_types=ocr_types,
                    selected_visual_feature_types=visual_feature_types,
                )
            # Issue#199：失败清单**无条件写**（含空 dict）——深合并语义下，重跑全成功
            # 必须清掉上一次的失败键，否则 reject→重跑留下假警示（冷审 I1）。
            self._write_failed_pages(store, task.item_id, failed_pages)
            if failed_pages:
                failed_ids = ", ".join(str(p) for p in sorted(failed_pages))
                summary = (
                    f"{len(failed_pages)} page(s) failed recognition "
                    f"(pages {failed_ids}); see recognition.failed_pages"
                )
                if len(summary) > 480:
                    summary = summary[:480] + "…"
                logger.warning(
                    "[queue] item=%s vision completed with %d failed page(s): %s",
                    task.item_id[:8],
                    len(failed_pages),
                    failed_ids,
                )
                # error_message 挂在 AWAITING_REVIEW 条目上是既有合法形态
                # （job_management_service 同类先例）；它会活不过打码完成迁移，
                # 跨迁移的权威载体是上面的 performance.failed_pages。
                store.set_item_error_message(task.item_id, summary)
            store.update_item_progress(
                task.item_id,
                stage="vision",
                current=pages,
                total=pages,
                message=(
                    "vision_recognition_complete_with_failures"
                    if failed_pages
                    else (
                        "vision_recognition_complete_visual_skipped"
                        if la_available is False
                        else "vision_recognition_complete"
                    )
                ),
            )
            self._record_item_performance(
                store,
                task.item_id,
                {
                    "recognition": {
                        "vision_ms": _elapsed_ms(vision_started),
                        "max_active_pages": max_active_pages,
                    }
                },
            )
            logger.info(
                "[queue] item=%s vision total elapsed=%.2fs pages=%d page_concurrency=%d max_active_pages=%d",
                task.item_id[:8],
                time.perf_counter() - vision_started,
                pages,
                page_concurrency,
                max_active_pages,
            )
        # 页级隔离后本包装只剩基础设施超时（store/调度层）的防御语义——页超时
        # 已在 run_page 内就地隔离（Issue#199），不再走到这里。Issue#139 的两类
        # 分立说明仍适用于本捕获（3.10 上 asyncio.TimeoutError 需显式列出）。
        except (TimeoutError, asyncio.TimeoutError) as exc:  # noqa: UP041
            raise TimeoutError(
                f"vision recognition timed out after {page_timeout:.0f}s per page"
            ) from exc

    async def _run_ner_or_vision(self, task: TaskItem, cfg: dict) -> None:
        """Choose text or vision recognition based on file type."""
        from app.services.file_operations import get_file_info

        fi = get_file_info(task.file_id) or {}
        ft = str(fi.get("file_type", ""))
        is_img = ft == "image" or bool(fi.get("is_scanned"))

        if is_img:
            await self._run_vision(task, cfg)
        else:
            entity_type_ids = list(cfg.get("entity_type_ids") or [])
            await self._run_ner(task, entity_type_ids)

    async def _run_vl_md(self, task: TaskItem, cfg: dict) -> None:
        """VL-MD 脱敏管线(Issue #66/#50 T1):识别+替换+产物落盘一步到位,条目直达 COMPLETED。"""
        from app.services.job_models import JobItemStatus, JobStatus
        from app.services.vl_md_pipeline_service import get_vl_md_pipeline_service

        store = self._get_store()
        store.update_item_progress(
            task.item_id, stage="vl_md", current=0, total=1, message="vl_md_running",
        )
        from app.services.file_operations import get_file_info

        fi = get_file_info(task.file_id)
        if not fi:
            raise ValueError(f"file_id={task.file_id} not in file_store")
        svc = get_vl_md_pipeline_service()
        summary = await svc.process_file(fi, cfg, job_id=task.job_id)
        store.update_item_progress(
            task.item_id, stage="vl_md", current=1, total=1,
            message=f"vl_md_done entities={summary['entity_count']} {summary['verdict']}",
        )
        # 直达 COMPLETED:产物已落盘(file_store output_* 字段),无审阅/回写阶段
        store.update_item_status(task.item_id, JobItemStatus.COMPLETED)
        # job 终态即释放映射表内存态(含真实姓名,不永驻;评审 Important#1)
        row = store.get_job(task.job_id)
        if row and row.get("status") in (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            svc.release_job_context(task.job_id)
        logger.info(
            "[queue] item=%s vl-md done entities=%d rounds=%d %s",
            task.item_id[:8], summary["entity_count"], summary["rounds"], summary["verdict"],
        )

    async def _run_agent_md(self, task: TaskItem, cfg: dict) -> str:
        """供料批量识别段（Issue#196）：段落构建 → NER → 映射草稿落 file_store。

        幂等：file_store 已有合法 ``agent_md_draft``（watchdog 重入队/失败重试）
        则短路复用，不重跑 MinerU。返回 carrier（"mineru"|"text"）供性能记录。
        草稿里含真实姓名，与单文件线同等待遇：不出现在任何日志/进度消息里。
        """
        from app.services.agent_md_batch import load_draft, run_agent_md_stage1
        from app.services.file_management_service import _file_store_lock, file_store
        from app.services.file_operations import get_file_info

        store = self._get_store()
        fi = get_file_info(task.file_id)
        if not fi:
            raise ValueError(f"file_id={task.file_id} not in file_store")

        async with _file_store_lock:
            existing = file_store.get(task.file_id)
        if load_draft(existing) is not None:
            logger.info(
                "[queue] item=%s agent_md draft exists, skip stage1", task.item_id[:8],
            )
            return str((existing or {}).get("agent_md_draft", {}).get("carrier") or "text")

        def _on_parse(done: int, total: int) -> None:
            store.update_item_progress(
                task.item_id, stage="parsing", current=done, total=total,
                message="agent_md_parsing",
            )

        def _on_ner(done: int, total: int) -> None:
            store.update_item_progress(
                task.item_id, stage="ner", current=done, total=total,
                message="agent_md_ner_running",
            )

        draft = await run_agent_md_stage1(
            fi,
            cfg,
            owner_id=str(fi.get("owner_id") or "local_user"),
            on_parse_progress=_on_parse,
            on_ner_progress=_on_ner,
        )
        async with _file_store_lock:
            info = file_store.get(task.file_id)
            if info is None:
                info = dict(fi)
            info["agent_md_draft"] = draft
            file_store.set(task.file_id, info)
        logger.info(
            "[queue] item=%s agent_md stage1 done carrier=%s mapping=%d",
            task.item_id[:8], draft.get("carrier"), len(draft.get("mapping") or []),
        )
        return str(draft.get("carrier") or "text")

    def _mark_recognition_complete(self, task: TaskItem, job: dict, store: JobStore) -> None:
        """Mark recognition complete and optionally enqueue redaction."""
        from app.services.job_store import JobItemStatus
        from app.services.task_queue import TaskItem

        # Issue#199（门⑥ 二段 F7）：展示面摘要**只从权威清单派生**——旧实现把
        # update_item_status 的整列覆盖语义用「回读既有 error_message」绕过，同状态
        # 迁移是 no-op 时会把上一次的陈旧摘要带到干净条目（假阳性警示）。这里显式
        # 重算：有失败页 ⇒ 明确摘要；无失败页 ⇒ None（清空）。
        failed_pages, total_pages = _recognition_failure_state(store, task.item_id)
        # Issue#254：识别完成撞取消态——取消不打断在飞识别（#233 语义），完成回调
        # 若照常写 awaiting_review 会撞状态机（cancelled 不可达 awaiting_review），
        # 表现为 ERROR 堆栈 + item 永久卡取消态而文件快照带全部结果（用户视角的
        # 「识别完成却无法匿名化」）。此处显式落账：结果保留在 file_store，item
        # 维持取消态，留一条可读 WARNING；redact 侧（single_task_service）会以
        # 中文文案引导用户重新识别。
        current_item = store.get_item(task.item_id) or {}
        if current_item.get("status") == JobItemStatus.CANCELLED.value:
            logger.warning(
                "[queue] item=%s recognition finished but item is cancelled; "
                "results retained in file_store, item state unchanged (Issue#254)",
                task.item_id[:8],
            )
            return
        skip_review = bool(job.get("skip_item_review"))
        if skip_review:
            # Issue#199 守卫（冷审 C1a）：识别存在失败页的条目**不 honor**
            # skip_item_review——「无人在环直通打码」会把失败页当干净页出厂
            # （失败页零区域 = 零脱敏）。强制停审阅，由用户在知情后决策。
            if failed_pages:
                logger.warning(
                    "[queue] item=%s skip review suppressed: %d page(s) failed recognition (%s)",
                    task.item_id[:8],
                    len(failed_pages),
                    ",".join(sorted(failed_pages)),
                )
                skip_review = False

        store.update_item_status(
            task.item_id,
            JobItemStatus.AWAITING_REVIEW,
            error_message=_failed_pages_error_summary(failed_pages, total_pages),
        )

        if skip_review:
            # skip_item_review=true: 直接入队匿名化，不等人工审阅
            # 使用 enqueue() 而非直接 put_nowait()，确保去重逻辑一致
            logger.info("[queue] item=%s skip review, enqueue redaction", task.item_id[:8])
            self.enqueue(TaskItem(
                job_id=task.job_id, item_id=task.item_id,
                file_id=task.file_id, task_type="redaction",
            ))
        else:
            logger.info("[queue] item=%s awaiting_review", task.item_id[:8])


class StructuredPipelineMixin:
    """结构化数据集导出流水线。"""

    async def _run_structured(self, task: TaskItem) -> None:
        from app.services.job_store import JobItemStatus, JobStatus
        from app.services.structured_service import run_structured_job_item

        started = time.perf_counter()
        store = self._get_store()
        self._record_task_started(task, store)
        job = store.get_job(task.job_id)
        if not job:
            return
        item = store.get_item(task.item_id)
        if not item or item["status"] == JobItemStatus.COMPLETED.value:
            return
        if job.get("status") == JobStatus.CANCELLED.value:
            return

        cfg = json.loads(job.get("config_json") or "{}")
        owner_id = str(job.get("owner_id") or "local_user")
        export_format = str(cfg.get("export_format") or "csv")
        try:
            store.update_item_status(task.item_id, JobItemStatus.PROCESSING)
            self._try_update_job_status(store, task.job_id, JobStatus.PROCESSING)
            result = await run_structured_job_item(
                job_id=task.job_id,
                item_id=task.item_id,
                dataset_id=task.file_id,
                owner_id=owner_id,
                export_format=export_format,
                store=store,
            )
            self._record_item_performance(
                store,
                task.item_id,
                {
                    "structured": {
                        "finished_at": _utc_iso(),
                        "duration_ms": result.get("duration_ms") or _elapsed_ms(started),
                        "export": result.get("export"),
                        "profile": result.get("profile"),
                    }
                },
            )
            store.update_item_status(task.item_id, JobItemStatus.COMPLETED)
            logger.info("[queue] item=%s structured export completed", task.item_id[:8])
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            err_msg = str(exc)[:_ERROR_MSG_MAX_LEN]
            logger.error("[queue] item=%s structured failed: %s", task.item_id[:8], err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except (KeyError, ValueError):
                pass
        except PdfEncryptedError:
            # Issue #32：结构化导出同样可能触及加密 PDF，上抛保持一致。
            raise
        except Exception as exc:
            err_msg = str(exc)[:_ERROR_MSG_MAX_LEN]
            logger.exception("[queue] item=%s structured failed (unexpected): %s", task.item_id[:8], err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except Exception:
                pass
        finally:
            self._record_item_performance(
                store,
                task.item_id,
                {"structured": {"last_seen_duration_ms": _elapsed_ms(started)}},
            )
            store.touch_job_updated(task.job_id)
            self._refresh_job_status(store, task.job_id)


class RedactionPipelineMixin:
    """匿名化（脱敏）流水线。"""

    async def _run_redaction(self, task: TaskItem) -> None:
        from app.models.schemas import RedactionConfig, ReplacementMode
        from app.services.file_operations import execute_redaction_request, get_file_info
        from app.services.job_store import JobItemStatus

        started = time.perf_counter()
        store = self._get_store()
        self._record_task_started(task, store)
        job = store.get_job(task.job_id)
        if not job:
            return

        item = store.get_item(task.item_id)
        if not item or item["status"] == JobItemStatus.COMPLETED.value:
            return

        cfg = json.loads(job.get("config_json") or "{}")

        try:
            store.update_item_status(task.item_id, JobItemStatus.PROCESSING)

            fi = get_file_info(task.file_id)
            if not fi:
                raise RuntimeError(f"file not found: {task.file_id}")
            if fi.get("output_path"):
                # 已匿名化
                self._record_item_performance(
                    store,
                    task.item_id,
                    {
                        "redaction": {
                            "finished_at": _utc_iso(),
                            "duration_ms": _elapsed_ms(started),
                            "skipped_existing_output": True,
                        }
                    },
                )
                store.update_item_status(task.item_id, JobItemStatus.COMPLETED)
                return

            # Issue#238：打码段进度——此前整卷一次性执行零上报，大卷渲染数分钟
            # 用户只能盯着「正在匿名化...」。起跑先写 0/N（前端立即出「打码中」），
            # 扫描件/图片栅格化路径逐页回调上报；文本/docx 快链路无页级回调，
            # 前端回落不定态。page_count<=0（docx/txt 等无页概念）不传回调。
            redaction_pages = int(fi.get("page_count") or 0)
            store.update_item_progress(
                task.item_id,
                stage="redaction",
                current=0,
                total=redaction_pages,
                message="redaction_started",
            )
            reported_redaction_page = 0

            def redaction_progress_cb(current: int, total: int) -> None:
                nonlocal reported_redaction_page
                if current <= reported_redaction_page:
                    return
                reported_redaction_page = current
                store.update_item_progress(
                    task.item_id,
                    stage="redaction",
                    current=current,
                    total=total,
                    message=f"打码 page {current}/{total}",
                )

            from app.models.schemas import BoundingBox, Entity
            raw_ents = fi.get("entities") or []
            entities = []
            for e in raw_ents:
                if isinstance(e, Entity):
                    entities.append(e)
                elif isinstance(e, dict):
                    entities.append(Entity.model_validate(e))

            raw_boxes = fi.get("bounding_boxes")
            boxes = []
            if isinstance(raw_boxes, list):
                for b in raw_boxes:
                    if isinstance(b, dict):
                        boxes.append(BoundingBox.model_validate(b))
            elif isinstance(raw_boxes, dict):
                for pk, arr in raw_boxes.items():
                    page_num = int(pk) if str(pk).isdigit() else 1
                    if isinstance(arr, list):
                        for b in arr:
                            if isinstance(b, dict):
                                d = {**b, "page": b.get("page", page_num)}
                                boxes.append(BoundingBox.model_validate(d))

            rm = cfg.get("replacement_mode") or "structured"
            try:
                replacement_mode = ReplacementMode(str(rm))
            except ValueError:
                replacement_mode = ReplacementMode.STRUCTURED

            config = RedactionConfig(
                replacement_mode=replacement_mode,
                entity_types=list(cfg.get("entity_type_ids") or []),
                custom_replacements=dict(cfg.get("custom_replacements") or {}),
                image_redaction_method=cfg.get("image_redaction_method"),
                image_redaction_strength=int(cfg.get("image_redaction_strength") or 75),
                image_fill_color=str(cfg.get("image_fill_color") or "#000000"),
                watermark_text=(str(cfg.get("watermark_text") or "").strip() or None),
                watermark_delivery=cfg.get("watermark_delivery"),
            )
            await execute_redaction_request(
                task.file_id,
                entities,
                boxes,
                config,
                progress_cb=redaction_progress_cb if redaction_pages > 0 else None,
            )
            self._record_item_performance(
                store,
                task.item_id,
                {
                    "redaction": {
                        "finished_at": _utc_iso(),
                        "duration_ms": _elapsed_ms(started),
                        "entity_count": len(entities),
                        "bounding_box_count": len(boxes),
                    }
                },
            )
            store.update_item_status(task.item_id, JobItemStatus.COMPLETED)
            logger.info("[queue] item=%s redaction completed", task.item_id[:8])

        except (FileNotFoundError, OSError) as e:
            err_msg = str(e)[:_ERROR_MSG_MAX_LEN]
            logger.error("[queue] item=%s redaction I/O error: %s", task.item_id[:8], err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except (KeyError, ValueError):
                pass
        except (RuntimeError, ValueError, KeyError) as e:
            err_msg = str(e)[:_ERROR_MSG_MAX_LEN]
            logger.error("[queue] item=%s redaction failed: %s: %s", task.item_id[:8], type(e).__name__, err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except (KeyError, ValueError):
                pass
        except PdfEncryptedError:
            # Issue #32：同识别流水线——原样上抛给 worker 顶层写友好中文消息。
            raise
        except Exception as e:
            err_msg = str(e)[:_ERROR_MSG_MAX_LEN]
            logger.exception("[queue] item=%s redaction failed (unexpected): %s", task.item_id[:8], err_msg)
            try:
                store.update_item_status(task.item_id, JobItemStatus.FAILED, error_message=err_msg)
            except Exception:
                pass
        finally:
            self._record_item_performance(
                store,
                task.item_id,
                {"redaction": {"last_seen_duration_ms": _elapsed_ms(started)}},
            )
            store.touch_job_updated(task.job_id)
            self._refresh_job_status(store, task.job_id)
