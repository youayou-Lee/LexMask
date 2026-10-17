"""Issue#165：`pipeline_status["text_layer_doc"]` 的构造必须是可单测的纯函数。

`detect_vision` 需要文件仓库 + 整个视觉栈，本机不可驱动（既有 Issue#148 用例也把
该观测面留给云门④），故把那一小段判定抽成 `_text_layer_doc_status()` 并在此钉住：
失败卷要带**机读原因码**，健康卷**不得**凭空多出回落原因。
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from app.services import text_layer_docline_service as docline
from app.services.redaction_orchestrator import _text_layer_doc_status


def test_failure_volume_block_carries_reason_code():
    marker = {
        "unusable": True,
        "retry_at": float("inf"),
        "reason": "failed_chunks:1/2",
        "reason_code": "failed_chunks",
        "reason_detail": {"failed_chunks": 1, "chunks": 2},
    }
    status = _text_layer_doc_status(None, marker, page=3)
    assert status["ran"] is False, "失败卷不得写成「接管了」"
    assert status["failed"] is True
    assert status["fallback_reason_code"] == "failed_chunks", status
    assert status["fallback_reason"] == "failed_chunks:1/2"
    assert status["fallback_failed_chunks"] == 1 and status["fallback_chunks"] == 2
    assert status["fallback_page"] == 3, "必须指名是哪一页回的落"
    # 未修版本上本函数不存在（那一块整块消失）或在失败卷上根本不写出 ⇒ 非恒真。


def test_healthy_volume_block_has_no_fallback_reason():
    memo = {
        "pages": 2, "entities": 7, "chunks": 3, "coverage": 0.98,
        "verdict": "usable", "seconds": 1.5, "http_errors": 0, "truncated_chunks": 0,
    }
    memo.update({"pages": 2, "entities": 7, "chunks": 3, "coverage": 0.98,
                 "verdict": "usable", "seconds": 1.5, "http_errors": 0, "truncated_chunks": 0})
    status = _text_layer_doc_status([object()], memo, page=1)
    assert status["ran"] is True and status["failed"] is False
    assert status["fallback_reason_code"] is None and status["fallback_reason"] is None
    # r1 冷审（AC2 逐字段相等过窄）：**十个旧字段逐个**与 memo 同值，少一个即判红；
    # 仅断言其中两个会让「成功块丢掉 coverage/verdict/chunks/seconds」的变异存活。
    missing = {k: (status.get(k), memo.get(k)) for k in LEGACY_COPIED_FIELDS if status.get(k) != memo.get(k)}
    assert not missing, f"成功接管块的旧字段必须逐字段等于 memo：{missing}"


# `ran`/`failed` 是**由 doc_boxes/doc_memo 计算**的（`failed` 还含 http_errors/truncated 兜底），
# 其余八个旧字段是**逐字段搬运** memo ⇒ 只有后者能拿「相等」钉。
LEGACY_COPIED_FIELDS = ("pages", "entities", "chunks", "coverage",
                        "verdict", "seconds", "http_errors", "truncated_chunks")


def test_partial_failure_marks_failed_but_keeps_counts():
    memo = {
        "pages": 1, "entities": 4, "chunks": 3, "coverage": 0.9, "verdict": "usable",
        "seconds": 0.7, "http_errors": 1, "truncated_chunks": 0,
    }
    status = _text_layer_doc_status([object()], memo, page=1)
    assert status["ran"] is True and status["failed"] is True, "有 HTTP 失败块就必须标 failed"
    assert status["http_errors"] == 1 and status["fallback_reason_code"] is None


def test_docline_has_no_dead_failed_chunks_counter():
    """唯一真源 = `len(failed_set)`；那个全仓无读者的 `stats["failed_chunks"]` 不得复活。"""
    src = inspect.getsource(docline)
    # 只看**会被执行的形式**（注释里可以提这件事，那不是第二个口径）
    assert 'stats["failed_chunks"] += 1' not in src, "死计数器被重新引入（两个口径会漂移）"
    assert '"failed_chunks": 0' not in src, "stats 初始化里又出现了第二个口径"


def test_docline_stats_dict_has_no_failed_chunks_key():
    """AC4① 的结构化判据：死计数器不得以**任何**写法回到 stats 字典里。

    r1 冷审自造变异：用非字面量的 `stats["failed_chunks"] = 0` 复活死计数器时，
    原先基于字符串 grep 的判据 SURVIVED。这里改走 AST，直接看 `run_document_ner`
    里 `stats = {...}` 的**键集合** ⇒ 字面量/非字面量一视同仁。
    """
    src = pathlib.Path(docline.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    keys_written: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            # 形态一：`stats = {...}` 字典字面量
            if (isinstance(target, ast.Name) and target.id == "stats"
                    and isinstance(node.value, ast.Dict)):
                keys_written += [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            # 形态二：`stats["failed_chunks"] = 0`（非字面量写法，r1 冷审的 M-C2 正是这样存活的）
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and target.value.id == "stats" and isinstance(target.slice, ast.Constant)):
                keys_written.append(target.slice.value)
    assert keys_written, "必须在 text_layer_docline_service 里找到 `stats` 的初始化"
    assert "failed_chunks" not in keys_written, f"stats 不得再写 failed_chunks 死计数器：{keys_written}"
