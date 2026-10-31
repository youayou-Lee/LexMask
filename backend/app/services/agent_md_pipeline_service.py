"""Issue#75 管线服务：状态机 + 两阶段编排（解析→NER→映射草稿；确认→出稿落盘）。

状态机约定：
- ``create_task``：同步登记任务（PARSING）后以 ``asyncio.ensure_future`` 后台驱动
  Stage1，立即返回，不等待解析完成；
- ``confirm``：仅 MAPPING_READY 可确认；否则任务置 FAILED（message="task not ready"）；
- ``confirm`` 收到未知 task_id 时抛 ``ValueError("unknown task: ...")``——router 层
  应先用 ``get_task`` 拦截（404），不应依赖此异常；
- ``get_task`` 未知 task_id 返回 None。

依赖注入：``mineru_client`` / ``ocr`` / ``ner`` 构造参数，缺省时惰性构建真实单例
（``_default_*`` 全部函数内 import，避免模块导入期拉起真实依赖）。``entity_types``
显式传入时原样透传给 NER；缺省空列表则 Stage1 起点按 #66 同款口径
（``resolve_requested_entity_types`` × 默认九类）解析 owner 缺省启用类型（终审 C2）。
"""
import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from app.services import file_management_service as fms
from app.services.agent_md_adapter import (
    CleanStats,
    clean_segments,
    enrich_image_blocks,
    markdown_body_chars,
    normalize_content_list,
    normalize_docvortex,
)
from app.services.agent_md_ner import build_mapping_draft, chunk_segments, run_ner
from app.services.agent_md_replace import apply_decisions, render_outputs
from app.services.agent_md_types import AgentMdTask, Seg, TaskState

# Issue#212：两版正文量相差超过 5%（= 比值低于 0.95）即视为某一版丢了东西。
# 阈值来源：修复前真实案卷 v2/v1 = 138/187,119 = 0.0007、合成夹具 33/258 = 0.128，
# 修复后两路应逐段一致（比值 1.0）；取 0.95 只在"确实少了一截"时触发。
GUARD_RATIO = 0.95

# Issue#219（I2）：4.x 单表示对 zip 内官方 .md 的比值守卫。探针锚点 0.9684
# （canary-issue219 三页样本 v4=429 / md=443，2026-11-01 实测）；取 0.7——下方
# 余量覆盖 md 剥除口径噪声（HTML 实体/页注释/图片 alt），上方紧到能抓住整类
# 部件丢失（丢 text 部件类 ⇒ 比值趋 0）。门④ 端到端复测，跌破须修映射或重锚。
DOC_VORTEX_MD_RATIO = 0.7


def _body_chars(segs: list[Seg]) -> int:
    """正文量口径：只数真正进 NER 的 text/table 段（哨兵/OCR 占位不计）。"""
    return sum(len(s.text) for s in segs if s.source in ("text", "table"))


class AgentMdPipelineService:
    """喂Agent模式管线：解析（MinerU）→ NER → 映射草稿 → 确认 → 出稿落盘。"""

    def __init__(
        self,
        mineru_client=None,
        ocr=None,
        ner=None,
        entity_types: list | None = None,
        output_dir: str | None = None,
    ):
        self.mineru = mineru_client if mineru_client is not None else _default_mineru()
        self.ocr = ocr if ocr is not None else _default_ocr()
        self.ner = ner if ner is not None else _default_ner()
        self.entity_types = list(entity_types) if entity_types else []
        self.output_dir = Path(output_dir or settings.OUTPUT_DIR)
        self._tasks: dict[str, AgentMdTask] = {}
        self._lock = asyncio.Lock()  # 全局串行解析（spec §1）
        self._sync_loop: asyncio.AbstractEventLoop | None = None  # 仅 *_nowait 测试驱动用

    # ---- 对外（router）----
    async def create_task(
        self, file_path: str, filename: str, owner_id: str = "local_user"
    ) -> AgentMdTask:
        """登记任务（PARSING）并后台启动 Stage1，立即返回。"""
        task = self._register(file_path, filename, owner_id)
        asyncio.ensure_future(self._run_stage1(task))
        return task

    def get_task(self, task_id: str) -> AgentMdTask | None:
        return self._tasks.get(task_id)

    async def confirm(self, task_id: str, decisions: list[dict]) -> AgentMdTask:
        """Stage2：应用决策→占位替换出稿→三件套落盘→file_store 登记→COMPLETED。

        未知 task_id 抛 ValueError（router 先 get_task 拦截）；非 MAPPING_READY
        时任务置 FAILED（message="task not ready"）并原样返回。
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")
        if task.state != TaskState.MAPPING_READY:
            task.state = TaskState.FAILED
            task.message = "task not ready"
            return task
        apply_decisions(task.mapping, decisions)
        md, mapping_json, retained_json = render_outputs(task.segs, task.entities, task.mapping)
        output_file_id = str(uuid.uuid4())
        stem = _friendly_stem(task.filename)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        short4 = output_file_id[:4]
        md_path = self.output_dir / f"{stem}_脱敏MD_{ts}_{short4}.md"
        mapping_path = self.output_dir / f"{stem}_映射表_{ts}_{short4}.json"
        retained_path = self.output_dir / f"{stem}_保留清单_{ts}_{short4}.json"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
        mapping_path.write_text(
            json.dumps(mapping_json, ensure_ascii=False, indent=2), encoding="utf-8")
        retained_path.write_text(
            json.dumps(retained_json, ensure_ascii=False, indent=2), encoding="utf-8")
        task.output_file_id = output_file_id
        task.state = TaskState.COMPLETED
        # file_store 登记（orphan 清理尊重 file_store 引用，main.py cleanup_orphan_files；
        # 键名必须是 vl_md_meta——main.py _known_file_store_paths 只认 output_path+vl_md_meta，
        # 终审 C3：旧键 agent_md_meta 不被识别，mapping/retained 会被孤儿清理删除）
        fms.file_store[output_file_id] = {
            "file_id": output_file_id,
            "filename": md_path.name,
            # original_filename 镜像：处理历史列表（GET /files）与行下载文件名都读这个键
            "original_filename": md_path.name,
            "output_path": str(md_path),
            "owner_id": task.owner_id,
            "vl_md_meta": {
                "mapping_path": str(mapping_path),
                "retained_path": str(retained_path),
            },
        }
        return task

    # ---- 测试驱动（同步骨架）----
    def create_task_nowait(
        self, file_path: str, filename: str, owner_id: str = "local_user"
    ) -> AgentMdTask:
        """仅测试用：同步驱动完整 Stage1。"""
        task = self._register(file_path, filename, owner_id)
        return self._run_sync(self._run_stage1(task))

    def confirm_nowait(self, task_id: str, decisions: list[dict]) -> AgentMdTask:
        """仅测试用：同步驱动 confirm。"""
        return self._run_sync(self.confirm(task_id, decisions))

    # ---- 内部 ----
    def _register(self, file_path: str, filename: str, owner_id: str) -> AgentMdTask:
        task = AgentMdTask(
            task_id=str(uuid.uuid4()), state=TaskState.PARSING,
            stage="mineru", file_path=file_path, filename=filename, owner_id=owner_id,
        )
        self._tasks[task.task_id] = task
        return task

    def _run_sync(self, coro):
        """在服务私有事件循环上跑协程（复用同一循环，保持 Lock 绑定一致）。"""
        if self._sync_loop is None or self._sync_loop.is_closed():
            self._sync_loop = asyncio.new_event_loop()
        return self._sync_loop.run_until_complete(coro)

    async def _run_stage1(self, task: AgentMdTask) -> AgentMdTask:
        """Stage1：读文件→MinerU 解析（全局串行锁）→清洗→NER→映射草稿。"""
        # 评审 rider：Stage1 进行中（等锁/解析/NER 的任一 await 窗口）confirm 都会把
        # 任务判 FAILED（"task not ready"）——此后状态机不得再推进（复活会导致迟到
        # 的 confirm 绕过就绪校验出稿）。was_failed 覆盖等锁窗口被 line 内
        # ``task.state = PARSING/NER_RUNNING`` 覆写丢失的情况。
        was_failed = task.state == TaskState.FAILED
        dead = False  # 终审 I1：锁内任一 await 窗口后被判死，后续一律不覆写不推进
        try:
            async with self._lock:
                file_bytes = Path(task.file_path).read_bytes()
                task.pages_total = max(1, _pdf_page_count(file_bytes))
                job_id = await self.mineru.submit(file_bytes=file_bytes, filename=task.filename)
                if task.state == TaskState.FAILED:  # submit 窗口内被判死不得覆写回 PARSING
                    dead = True
                else:
                    task.state = TaskState.PARSING

                def on_progress(done: int, total: int) -> None:
                    task.pages_done, task.pages_total = done, total

                result = await self.mineru.wait_result(job_id, on_progress=on_progress)
            if dead or task.state == TaskState.FAILED or was_failed:
                return task
            task.state = TaskState.NER_RUNNING
            task.stage = "ner"
            # 终审 C1：sidecar v2 为外层按页嵌套形态（Task 0 §6c），先归一化成扁平 v1
            segs, warns, dropped, source = self._choose_representation(result)
            task.dropped_blocks = {
                "empty_table": dropped.empty_table_blocks,
                "empty_table_pages": list(dropped.empty_table_pages),
                "unhandled": dict(dropped.dropped_block_types),
                "source": source,
            }
            if segs is None:  # 两路都解析不出正文：显式失败，禁"0 映射静默就绪"
                task.state = TaskState.FAILED
                # Issue#219：失败形态区分（AC5 矩阵）——4.x 单路空 / 未知形状点名 /
                # 3.x 双路空，机读原因码统一 parse_no_text（前端零改动）
                if getattr(result, "mineru4_docs", None):
                    task.message = ("parse_no_text: MinerU 4.x 产物未解析出任何正文"
                                    "（docvortex_middle 路 0 字符或文档 pages 不一致）")
                elif getattr(result, "unknown_jsons", None):
                    # 只点名文件名（owner 自见面）；产物内容不进任何告警
                    task.message = ("parse_no_text: MinerU 产物 schema 无法识别"
                                    f"（未知 JSON: {', '.join(result.unknown_jsons)}）")
                else:
                    task.message = "parse_no_text: MinerU 未解析出任何正文（v1/v2 两路均 0 字符）"
                task.warnings.extend(warns)
                return task
            task.chars = _body_chars(segs)
            segs, more = enrich_image_blocks(segs, result.images, self.ocr)
            task.warnings.extend(warns)
            task.warnings.extend(more)
            task.segs = segs
            chunks = chunk_segments(segs)
            # run_ner 就地平移 Entity 偏移——每任务只调一次
            entity_types = self.entity_types or self._resolve_entity_types(task.owner_id)
            task.entity_types_used = [str(t) for t in entity_types]
            entities, _ = await run_ner(chunks, entity_types, self.ner)
            task.entities = entities
            task.mapping = build_mapping_draft(entities)
            if task.state == TaskState.FAILED:  # NER await 窗口内被判死同样不复活
                return task
            task.state = TaskState.MAPPING_READY
        except Exception as exc:  # noqa: BLE001 —— 单任务失败不拖垮服务
            task.state = TaskState.FAILED
            task.message = f"{type(exc).__name__}: {exc}"
        return task

    @staticmethod
    def _choose_representation(result) -> tuple[list[Seg] | None, list[str], CleanStats, str]:
        """Issue#212 双表示守卫 + Issue#219 4.x 分流：按产物 schema 选表示并告警。

        返回 (segs, warns, 观测侧 stats, 命中来源)；选不出正文时 segs 为 None
        （调用方判 FAILED，按来源写 parse_no_text 形态消息）。

        Issue#219 v4 分支（起手先探，优先于 3.x）：docvortex.middle 单表示，
        不走 v1/v2 双表示守卫（守卫空转）——但以 zip 内官方 .md 为参照做比值守卫
        ``docvortex_shortfall``（无回落可用，warning-only）；文档 pages 去重后
        仍 >1 份 ⇒ fail-closed。

        3.x 双表示守卫（Issue#212，逐字保持）：
        - v1 缺失（客户端未拿到）→ 用 v2 + `dual_representation_unavailable`；
        - v2 丢表或整体归零，且正文明显少于 v1 → 回落 v1（`winner=content_list_v1`）；
        - v1 反而更多但 v2 没有丢表（典型=水印/重复放大把 v1 撑大）→ 留在 v2；
        - 两路接近 → 用 v2（管线主路径），不告警。
        """
        if getattr(result, "mineru4_docs", None):
            flat, norm_warns = normalize_docvortex(result.mineru4_docs)
            v4_stats = CleanStats()
            v4_segs, v4_warns = clean_segments(flat, stats=v4_stats)
            # 门⑥ M-4：归一层告警与 clean 层告警统一 v4: 前缀（grep 取数一个口径）
            warns = [f"v4: {w}" for w in norm_warns]
            if getattr(result, "unknown_jsons", None):
                warns.append(f"schema_unknown_json: {', '.join(result.unknown_jsons)}")
            if getattr(result, "content_list_v1", None) or getattr(result, "content_list_v2", None):
                warns.append("mixed_schema_v4_wins: zip 同时含 4.x 与 3.x 表示，已按 4.x 解析")
            if any("mineru4_multiple_docs" in w for w in norm_warns):
                # pages 去重后仍 >1 份（R1）：fail-closed，盲拼会把导出 md 全文翻倍；
                # 门⑥ M-1：失败路径也要带上 clean 层告警（观测面不缺一截）
                warns.extend(f"v4: {w}" for w in v4_warns)
                return None, warns, v4_stats, "docvortex_middle"
            if getattr(result, "content_list_v1", None) or getattr(result, "content_list_v2", None):
                warns.append("mixed_schema_v4_wins: zip 同时含 4.x 与 3.x 表示，已按 4.x 解析")
            v4_chars = _body_chars(v4_segs)
            if v4_chars == 0:
                warns.extend(f"v4: {w}" for w in v4_warns)
                return None, warns, v4_stats, "docvortex_middle"
            md = getattr(result, "md", "") or ""
            md_chars = markdown_body_chars(md) if md.strip() else 0
            if md_chars == 0:
                # R4 + 门⑥ M-2：md 缺位与 md 无正文同一显式语义——守卫跳过必须留痕
                warns.append("docvortex_md_missing: zip 内 .md 缺失或无正文字符，比值守卫跳过")
            elif v4_chars < DOC_VORTEX_MD_RATIO * md_chars:
                    warns.append("docvortex_shortfall: v4 正文显著少于 zip 内官方 md "
                                 f"v4={v4_chars} md={md_chars}")
            warns.extend(f"v4: {w}" for w in v4_warns)
            return v4_segs, warns, v4_stats, "docvortex_middle"

        warns: list[str] = []
        if getattr(result, "unknown_jsons", None):
            # 对称行为（R3）：3.x 有正文仍出稿，但未知形状必须点名不静默
            warns.append(f"schema_unknown_json: {', '.join(result.unknown_jsons)}")
        v2_stats = CleanStats()
        v2_segs, v2_warns = clean_segments(
            normalize_content_list(result.content_list_v2), stats=v2_stats)
        v1_stats = CleanStats()
        v1_segs, v1_warns = clean_segments(
            normalize_content_list(result.content_list_v1), stats=v1_stats)
        v2_chars, v1_chars = _body_chars(v2_segs), _body_chars(v1_segs)

        # 门⑥ I6：两路皆空必须判"没解析出正文"——即使 v1 缺失也不能当"识别完 0 实体"
        if v1_chars == 0 and v2_chars == 0:
            if getattr(result, "unknown_jsons", None):
                return None, warns, v2_stats, "unknown"  # AC5③：点名形态
            return None, warns, v2_stats, "none"
        if not result.content_list_v1:
            warns.append("dual_representation_unavailable: mineru 产物缺 v1 content_list")
            warns.extend(f"v2: {w}" for w in v2_warns)
            return v2_segs, warns, v2_stats, "content_list_v2"
        if v1_chars == 0:
            warns.append(
                f"dual_representation_shortfall: winner=content_list_v2 v1=0 v2={v2_chars}")
            warns.extend(f"v2: {w}" for w in v2_warns)
            return v2_segs, warns, v2_stats, "content_list_v2"
        if v2_chars == 0:
            warns.append(
                f"dual_representation_shortfall: winner=content_list_v1 v1={v1_chars} v2=0")
            warns.extend(f"v1: {w}" for w in v1_warns)
            return v1_segs, warns, v2_stats, "content_list_v1"
        short = v2_chars < GUARD_RATIO * v1_chars
        if short and v2_stats.empty_table_blocks > 0:
            warns.append("dual_representation_shortfall: winner=content_list_v1 "
                         f"v1={v1_chars} v2={v2_chars}")
            warns.extend(f"v1: {w}" for w in v1_warns)
            return v1_segs, warns, v2_stats, "content_list_v1"
        if short:
            # v2 没丢表但正文比 v1 少一截：多半是 v1 侧被水印/重复文本撑大，不盲取较大者
            warns.append("dual_representation_shortfall: winner=content_list_v2 "
                         f"v1={v1_chars} v2={v2_chars}")
            warns.extend(f"v2: {w}" for w in v2_warns)
            return v2_segs, warns, v2_stats, "content_list_v2"
        if v1_chars < GUARD_RATIO * v2_chars:
            warns.append("dual_representation_shortfall: winner=content_list_v2 "
                         f"v1={v1_chars} v2={v2_chars}")
        warns.extend(f"v2: {w}" for w in v2_warns)
        return v2_segs, warns, v2_stats, "content_list_v2"

    @staticmethod
    def _resolve_entity_types(owner_id: str | None) -> list:
        """缺省实体类型解析（终审 C2）：与 #66 同款口径——默认九类清单经
        ``resolve_requested_entity_types`` 按 owner 三态配置过滤（停用不识别、
        自定义项一等公民）。显式构造参数优先，仅在 entity_types 为空时调用。"""
        from app.services.entity_type_service import resolve_requested_entity_types
        from app.services.vl_md_pipeline_service import VL_MD_DEFAULT_TYPE_IDS

        return resolve_requested_entity_types(list(VL_MD_DEFAULT_TYPE_IDS), owner_id or None)


_ILLEGAL_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')


def _friendly_stem(filename: str) -> str:
    """原文件名 → 导出文件名前缀:去扩展名、非法字符与路径分隔符→下划线、
    空白折叠、截断 60 字符;清空后兜底 "document"。"""
    stem = (filename or "").rsplit(".", 1)[0] if filename else ""
    stem = _ILLEGAL_FILENAME_CHARS.sub("_", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    stem = stem[:60].strip()
    return stem or "document"


def _pdf_page_count(pdf_bytes: bytes) -> int:
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc.page_count


def _default_mineru():
    from app.services.mineru_parse_client import MineruParseClient

    return MineruParseClient()


def _default_ocr():
    from app.services.ocr_service import ocr_service  # 该文件底部全局单例

    return ocr_service


def _default_ner():
    from app.services.hybrid_ner_service import HybridNERService

    return HybridNERService()


_pipeline: AgentMdPipelineService | None = None


def get_agent_md_pipeline() -> AgentMdPipelineService:
    """惰性单例工厂：首次调用才构建真实依赖（router 每请求调用；测试 monkeypatch 接缝）。"""
    global _pipeline
    if _pipeline is None:
        _pipeline = AgentMdPipelineService()
    return _pipeline
