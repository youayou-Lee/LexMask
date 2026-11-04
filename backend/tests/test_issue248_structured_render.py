"""Issue#248 供料产物结构保全：渲染层替换式改造（TDD 先红）。

设计（私有仓 spec 2026-11-05-issue248）：检测与应用分离——NER 检测照旧跑
拍平 segs；替换应用在 zip 官方结构化 md 上，表格/标题/页序原样保留。

三道机读闸：
1. 结构字符闸：replacement 含 ``|`` ``<`` ``>`` `` ` `` 换行 → 拒绝；
2. 结构对账：输入 md → 输出 md 逐表行/列、HTML 标签计数、页标记一致；
3. 零残留：非排除项 original_text 在最终 md 计数=0。
"""
import pytest

from app.models.entity_schemas import Entity
from app.services.agent_md_replace import (
    render_outputs_on_structure,
    verify_structure_integrity,
)
from app.services.agent_md_types import MappingItem, Seg

STRUCTURED_MD = (
    "<!-- page 1 of 2 -->\n\n"
    "# 财产查询反馈信息表\n\n"
    "<table><tr><td>被查询对象</td><td>韦某甲</td></tr>"
    "<tr><td>居民身份证</td><td>452626199005190011</td></tr></table>\n\n"
    "| 协执单位 | 账户余额 |\n"
    "| --- | --- |\n"
    "| 某银行 | 10.15 |\n\n"
    "<!-- page 2 of 2 -->\n\n"
    "正文段落：韦某甲名下房产一套。\n"
)

SEGS = [
    Seg(text="财产查询反馈信息表 被查询对象 韦某甲", page_idx=0, source="table"),
    Seg(text="居民身份证 452626199005190011", page_idx=0, source="table"),
    Seg(text="正文段落：韦某甲名下房产一套。", page_idx=1, source="text"),
]


def _mapping(**overrides):
    base = [
        MappingItem(id="m1", original_text="韦某甲", entity_type="PERSON",
                    replacement="韦某1"),
        MappingItem(id="m2", original_text="452626199005190011", entity_type="ID",
                    replacement="4526**********0011"),
    ]
    for m in base:
        for k, v in overrides.items():
            setattr(m, k, v)
    return base


def _entities():
    # 全局偏移按 "\n\n" 连接的 segs 计算（与检测层一致，门⑥ M1：真值锚定——
    # seg1="财产查询反馈信息表 被查询对象 韦某甲"（19 字符），韦某甲@[16,19)；
    # seg2 起点 21，证件号@[27,45)
    return [
        Entity(id="e1", text="韦某甲", type="PERSON", start=16, end=19),
        Entity(id="e2", text="452626199005190011", type="ID", start=27, end=45),
    ]


def test_structured_render_preserves_tables_and_replaces_values():
    md, mapping_json, retained = render_outputs_on_structure(
        STRUCTURED_MD, SEGS, _entities(), _mapping())
    # 表格结构逐字节保留
    assert '<td>被查询对象</td>' in md and '</table>' in md
    assert "| 协执单位 | 账户余额 |" in md and "| 某银行 | 10.15 |" in md
    assert "<!-- page 1 of 2 -->" in md and "<!-- page 2 of 2 -->" in md
    # 敏感值替换、事实值保留
    assert "韦某1" in md and "452626199005190011" not in md
    assert "4526**********0011" in md and "10.15" in md
    assert mapping_json["items"][0]["replacement"] == "韦某1"


def test_structured_render_matches_legacy_semantics_excluded_retained():
    mapping = _mapping()
    mapping[0].excluded = True  # 用户选择保留人名原文
    md, mapping_json, retained = render_outputs_on_structure(
        STRUCTURED_MD, SEGS, _entities(), mapping)
    assert "韦某甲" in md  # 原文保留
    assert "4526**********0011" in md  # 其余照常替换
    assert mapping_json["items"][0]["excluded"] is True
    pages = [r["page"] for r in retained["retained_fields"] if r["text"] == "韦某甲"]
    assert pages and set(pages) == {1}  # retained 按实体记账（页码=实体所在 seg 的 page_idx+1）


def test_excluded_entity_on_page2_derives_page2():
    """门⑥ M1：页码推导真判据——实体落在 seg2（page_idx=1）必须记页 2。"""
    mapping = _mapping()
    mapping[0].excluded = True
    ents = _entities()
    # 把人名实体挪到 seg2 的正文段「正文段落：韦某甲名下房产一套。」
    # seg2 全局起点 = 19(seg1) + 2 + 24(seg2) + 2 = 47；「正文段落：」5 字符
    # → 韦某甲@[52,55)，落在页 2（真值，门⑥ M1）
    ents[0] = Entity(id="e1", text="韦某甲", type="PERSON", start=52, end=55)
    _md, _mj, retained = render_outputs_on_structure(STRUCTURED_MD, SEGS, ents, mapping)
    pages = [r["page"] for r in retained["retained_fields"] if r["text"] == "韦某甲"]
    assert pages == [2]


def test_representation_mismatch_gate_rejects_unmatched_original():
    """门⑥ C1 前向覆盖闸：映射原文在结构化 md 中不存在 → 表示失配拒绝出厂。"""
    mapping = _mapping()
    mapping[1].original_text = "000000000000000000"  # 检测面有、产物面（md）没有
    with pytest.raises(ValueError, match="representation_mismatch"):
        render_outputs_on_structure(STRUCTURED_MD, SEGS, _entities(), mapping)


def test_replacement_structural_chars_rejected():
    mapping = _mapping()
    mapping[0].replacement = "韦某1|李某1"  # 竖线会破坏管道表格
    with pytest.raises(ValueError, match="replacement_structural_chars"):
        render_outputs_on_structure(STRUCTURED_MD, SEGS, _entities(), mapping)
    mapping[0].replacement = "韦某1\n第二行"
    with pytest.raises(ValueError, match="replacement_structural_chars"):
        render_outputs_on_structure(STRUCTURED_MD, SEGS, _entities(), mapping)


def test_zero_residual_violation_rejected():
    mapping = _mapping()
    mapping[0].replacement = "韦某1之韦某甲"  # 替换值回插原文 → 残留
    with pytest.raises(ValueError, match="zero_residual_violation"):
        render_outputs_on_structure(STRUCTURED_MD, SEGS, _entities(), mapping)


def test_verify_structure_integrity_passes_on_valid_output():
    md, _, _ = render_outputs_on_structure(STRUCTURED_MD, SEGS, _entities(), _mapping())
    verify_structure_integrity(STRUCTURED_MD, md)  # 不抛即过


def test_verify_structure_integrity_catches_html_row_loss():
    tampered = STRUCTURED_MD.replace("<tr><td>居民身份证</td><td>452626199005190011</td></tr>", "")
    with pytest.raises(ValueError, match="structure_integrity_violation"):
        verify_structure_integrity(STRUCTURED_MD, tampered)


def test_verify_structure_integrity_catches_pipe_row_loss():
    tampered = STRUCTURED_MD.replace("| 某银行 | 10.15 |\n", "")
    with pytest.raises(ValueError, match="structure_integrity_violation"):
        verify_structure_integrity(STRUCTURED_MD, tampered)


def test_verify_structure_integrity_catches_page_marker_loss():
    tampered = STRUCTURED_MD.replace("<!-- page 2 of 2 -->\n\n", "")
    with pytest.raises(ValueError, match="structure_integrity_violation"):
        verify_structure_integrity(STRUCTURED_MD, tampered)


def test_verify_structure_integrity_ignores_plain_text_changes():
    # 正文变化（脱敏替换本体）不算结构破坏
    verify_structure_integrity(STRUCTURED_MD, STRUCTURED_MD.replace("10.15", "99.99"))


# ---- 管线/批量接线级（confirm 路径选择与闸生效） --------------------------


def _v4_doc(blocks_by_page):
    """docvortex.middle 形状合成（与 #219/#224 夹具同构）。"""
    return {
        "schema": "docvortex.middle", "schema_version": "2.0",
        "metadata": {"producer": {"name": "mineru", "version": "4.1.3"}},
        "pages": [{"page_idx": idx, "blocks": blocks} for idx, blocks in blocks_by_page],
        "is_full_document": True,
    }


def _run_pipeline_confirm(structured_md: str, tmp_path):
    """单文件线 confirm 全链：假 mineru（含官方 md）→ Stage1 → confirm。"""
    import fitz

    from app.services.agent_md_pipeline_service import AgentMdPipelineService
    from app.services.mineru_parse_client import MineruResult

    doc = _v4_doc([(0, [{"type": "text", "index": 0, "bbox": [0, 0, 1, 1],
                         "content": [{"type": "text", "content":
                                      "正文段落：韦某甲名下房产一套。"}]}])])

    class _FakeMineru:
        async def submit(self, file_bytes, filename):
            return "job-1"

        async def wait_result(self, task_id, on_progress=None):
            if on_progress:
                on_progress(1, 1)
            return MineruResult(md=structured_md, mineru4_docs=[doc], images={})

    class _FakeNer:
        async def extract(self, text, entity_types):
            from app.models.entity_schemas import Entity

            idx = text.find("韦某甲")
            if idx < 0:
                return []
            return [Entity(id="韦某甲", text="韦某甲", type="PERSON",
                           start=idx, end=idx + 3)]

    pdf = tmp_path / "a.pdf"
    fdoc = fitz.open()
    fdoc.new_page()
    fdoc.save(str(pdf))
    fdoc.close()
    svc = AgentMdPipelineService(
        mineru_client=_FakeMineru(), ocr=_FakeOcrStub(), ner=_FakeNer(),
        entity_types=["PERSON"], output_dir=str(tmp_path),
    )
    task = svc.create_task_nowait(str(pdf), "a.pdf")
    assert task.state.name == "MAPPING_READY", (
        task.state, task.message, task.dropped_blocks, task.warnings)
    return svc.confirm_nowait(task.task_id, []), tmp_path


class _FakeOcrStub:
    def extract_text_from_image(self, *a, **k):
        return ""


def test_pipeline_confirm_renders_on_structured_md(tmp_path):
    """confirm 后产物=结构化 md 替换体：表格/页标记保留（#248 主路径生效）。"""
    from app.services.agent_md_replace import verify_structure_integrity

    done, tmp_path = _run_pipeline_confirm(STRUCTURED_MD, tmp_path)
    assert done.state.name == "COMPLETED", done.message
    md_files = list(tmp_path.glob("*_脱敏MD_*.md"))
    md = md_files[0].read_text(encoding="utf-8")
    assert "<table>" in md and "<!-- page 1 of 2 -->" in md
    # 假 NER 只检出人名：人名被替换、未检出值保留（检测质量归效果线，不在本闸）
    assert "韦某甲" not in md and "[韦某]" in md
    assert "452626199005190011" in md
    verify_structure_integrity(STRUCTURED_MD, md)  # 产物与输入结构对账通过


def test_pipeline_confirm_verify_gate_kills_structure_damage(tmp_path):
    """变异钉：管线 confirm 的 verify_structure_integrity 调用被删时此测试必须红。

    手工映射 original 含管道结构字符（"某银行 |" ）→ 渲染成功但产物管道表
    被破坏 → 只有 confirm 内的 verify 调用能拦住。
    """
    from app.services.agent_md_pipeline_service import AgentMdPipelineService
    from app.services.agent_md_types import AgentMdTask, MappingItem, TaskState

    svc = AgentMdPipelineService(output_dir=str(tmp_path))
    task = AgentMdTask(
        task_id="t2", state=TaskState.MAPPING_READY, filename="a.pdf",
        structured_md=STRUCTURED_MD,
        mapping=[MappingItem(id="m1", original_text="某银行 |", entity_type="ORG",
                             replacement="某银行")],
    )
    svc._tasks["t2"] = task
    done = svc.confirm_nowait("t2", [])
    assert done.state == TaskState.FAILED
    assert "structure_integrity_violation" in done.message
    assert not list(tmp_path.glob("*_脱敏MD_*.md"))


def test_pipeline_confirm_fails_loud_on_gate_violation(tmp_path):
    """映射替换值带结构字符 → confirm 显式 FAILED（产物拒绝出厂，不写盘）。"""
    from app.services.agent_md_pipeline_service import AgentMdPipelineService
    from app.services.agent_md_types import AgentMdTask, MappingItem, TaskState

    svc = AgentMdPipelineService(output_dir=str(tmp_path))
    task = AgentMdTask(
        task_id="t1", state=TaskState.MAPPING_READY, filename="a.pdf",
        structured_md=STRUCTURED_MD,
        mapping=[MappingItem(id="m1", original_text="韦某甲", entity_type="PERSON",
                             replacement="韦某1|坏")],
    )
    svc._tasks["t1"] = task
    done = svc.confirm_nowait("t1", [])
    assert done.state == TaskState.FAILED
    assert "replacement_structural_chars" in done.message
    assert not list(tmp_path.glob("*_脱敏MD_*.md"))  # 产物没落盘



# ---- 批量线接线钉（#248）：commit 走结构化 md，删除该路径时此测试必须红 ----


def test_batch_commit_renders_on_structured_md(tmp_path, monkeypatch):
    """批量 commit：draft 带 structured_md → 产物表格保留（不走拍平重建）。"""

    from app.services import agent_md_batch as batch

    fs_store: dict = {}

    class _FS:
        def get(self, file_id):
            return fs_store.get(file_id)

        def update(self, file_id, patch):
            fs_store.setdefault(file_id, {}).update(patch)

    class _Ocr:
        def extract_text_boxes(self, image_bytes):
            return []

    md = STRUCTURED_MD
    draft = batch.serialize_draft(
        segs=[Seg(text="正文段落：韦某甲名下房产一套。", page_idx=1, source="text")],
        entities=[Entity(id="e1", text="韦某甲", type="PERSON", start=5, end=8)],
        mapping=[MappingItem(id="m1", original_text="韦某甲", entity_type="PERSON",
                             replacement="韦某1")],
        carrier="mineru", structured_md=md,
    )
    fs_store["f1"] = {"file_id": "f1", "filename": "a.pdf", "agent_md_draft": draft}
    patch = batch.commit_agent_md_item(_FS().get("f1") | {"file_id": "f1"}, [])
    md_path = patch["output_path"]
    out = open(md_path, encoding="utf-8").read()
    assert "<table>" in out and "| 某银行 | 10.15 |" in out  # 结构保留
    assert "韦某甲" not in out and "韦某1" in out            # 替换生效
    verify_structure_integrity(md, out)


# ---- 门⑥ I2：回落路径判据（structured_md 缺失 → legacy + 显式 warning） ------


def test_pipeline_confirm_fallback_flat_when_md_missing(tmp_path):
    """structured_md 缺失（zip 无 .md）→ 回落 legacy 拍平重建 + 显式 warning。

    变异钉：删 confirm 回落 warning 或回落分支，此测试必须红。"""
    from app.services.agent_md_pipeline_service import AgentMdPipelineService
    from app.services.agent_md_types import AgentMdTask, MappingItem, TaskState

    svc = AgentMdPipelineService(output_dir=str(tmp_path))
    task = AgentMdTask(
        task_id="t3", state=TaskState.MAPPING_READY, filename="a.pdf",
        structured_md=None,  # zip 官方 md 缺位
        segs=[Seg(text="正文段落：韦某甲名下房产一套。", page_idx=0, source="text")],
        entities=[Entity(id="e1", text="韦某甲", type="PERSON", start=5, end=8)],
        mapping=[MappingItem(id="m1", original_text="韦某甲", entity_type="PERSON",
                             replacement="韦某1")],
    )
    svc._tasks["t3"] = task
    done = svc.confirm_nowait("t3", [])
    assert done.state == TaskState.COMPLETED
    assert any("structured_md_missing" in w for w in done.warnings)  # 显式不静默
    md = list(tmp_path.glob("*_脱敏MD_*.md"))[0].read_text(encoding="utf-8")
    assert "<table>" not in md and "韦某1" in md  # 拍平形状（legacy 产物）


def test_batch_commit_fallback_flat_when_md_missing(tmp_path, monkeypatch):
    """批量 commit structured_md 缺失 → legacy 拍平产物 + warning 留痕。"""
    from app.services import agent_md_batch as batch

    fs_store: dict = {}

    class _FS:
        def get(self, file_id):
            return fs_store.get(file_id)

        def update(self, file_id, patch):
            fs_store.setdefault(file_id, {}).update(patch)

    draft = batch.serialize_draft(
        segs=[Seg(text="正文段落：韦某甲名下房产一套。", page_idx=0, source="text")],
        entities=[Entity(id="e1", text="韦某甲", type="PERSON", start=5, end=8)],
        mapping=[MappingItem(id="m1", original_text="韦某甲", entity_type="PERSON",
                             replacement="韦某1")],
        carrier="mineru", structured_md=None,
    )
    fs_store["f1"] = {"file_id": "f1", "filename": "a.pdf", "agent_md_draft": draft}
    warnings: list[str] = []
    monkeypatch.setattr(batch.logger, "warning",
                        lambda msg, *a, **k: warnings.append(msg % a if a else msg))
    batch.commit_agent_md_item(_FS().get("f1") | {"file_id": "f1"}, [])
    assert any("structured_md missing" in w for w in warnings)  # 显式不静默


def test_batch_commit_gate_failure_raises_valueerror(tmp_path):
    """批量闸失败 → ValueError（error_message 通道）→ 调用方打回 AWAITING_REVIEW。

    门⑥ I2：#248 前批量 commit 的 ValueError 只有草稿缺失一种；闸失败共用
    同一通道（job_management_service 两条调用方捕获后 item 留在 AWAITING_REVIEW
    并落 error_message——接线由既有 worker 测试面覆盖，此处钉闸的 ValueError 契约）。"""
    from app.services import agent_md_batch as batch

    class _FS:
        def get(self, file_id):
            return fs_store.get(file_id)

    draft = batch.serialize_draft(
        segs=[Seg(text="正文段落：韦某甲名下房产一套。", page_idx=0, source="text")],
        entities=[Entity(id="e1", text="韦某甲", type="PERSON", start=5, end=8)],
        mapping=[MappingItem(id="m1", original_text="韦某甲", entity_type="PERSON",
                             replacement="韦某1")],
        carrier="mineru", structured_md=STRUCTURED_MD,
    )
    fs_store = {"f1": {"file_id": "f1", "filename": "a.pdf", "agent_md_draft": draft}}
    with pytest.raises(ValueError, match="replacement_structural_chars"):
        # 决策把人名替换值换成带竖线 → 渲染闸拒绝
        batch.commit_agent_md_item(
            dict(_FS().get("f1"), file_id="f1"),
            [{"id": "m1", "action": "custom", "replacement": "韦|某"}])


# ---- 门⑥ N1：img_ocr/sentinel 来源实体豁免前向覆盖闸 -----------------------


def test_img_ocr_entity_exempt_from_coverage_gate():
    """图片 OCR 实体的文本只在检测面（img_ocr seg）、不在 zip md——不构成失配。

    r1 C1 闸初版误杀此类（带文字图片案卷 confirm 必失败）；r2 N1 修正后
    该夹具必须通过。变异钉：删豁免分支，此测试必须红。"""
    md_no_img = "# 报告\n\n正文：无表格。\n"  # 图片文本不在 md
    segs = [
        Seg(text="![](images/img_0_0.jpg)", page_idx=0, source="text"),
        Seg(text="户名 李四四 账号 6222020200112233445", page_idx=0, source="img_ocr"),
        Seg(text="正文：无表格。", page_idx=0, source="text"),
    ]
    ents = [
        # e1 锚在 img_ocr seg：文本不在 md → 必须豁免
        Entity(id="e1", text="李四四", type="PERSON", start=30, end=33),
        # e2 锚在 text seg 且文本在 md → 正常覆盖
        Entity(id="e2", text="无表格", type="OTHER", start=41, end=44),
    ]
    mapping = [
        MappingItem(id="m1", original_text="李四四", entity_type="PERSON", replacement="李某2"),
        MappingItem(id="m2", original_text="无表格", entity_type="OTHER", replacement="[保留词]"),
    ]
    out, _mj, _rt = render_outputs_on_structure(md_no_img, segs, ents, mapping)
    assert "李某2" not in out and "李四四" not in out  # 图片文本本就不在产物，无泄漏
    assert "[保留词]" in out  # md 内实体照常替换
