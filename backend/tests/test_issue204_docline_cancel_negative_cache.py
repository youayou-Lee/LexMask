"""Issue#204 缺陷 B（取消侧）：文档级建链被取消后必须落负缓存，否则重试从零再建。

事故形态（#203 两次真实复现的放大机制）：`boxes_for_page` 的整卷建链在页级
`asyncio.wait_for(..., timeout=300)` 覆盖范围内；超时/取消以 `CancelledError`
（继承 BaseException）穿透 `except _TransientIneligible` / `except Exception`
两个 handler ⇒ memo/负缓存一个字都不写 ⇒ 用户重试无限从零再建，永远出不来结果。

修复语义：建链被取消 → 落**短 TTL 负缓存**（reason_code="cancelled"）→
**必须 re-raise**（取消传播不能吞）；下一次同 key 请求在 TTL 内直接命中负缓存
短路返回，不再重放 O(整卷) 的建链。TTL 过期后允许重建（故障可能已恢复，
与 `_NEGATIVE_TTL_SEC` 口径一致）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.services import text_layer_doc_source as source


def _snapshot(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    return {"file_path": str(path), "file_type": "pdf_scanned", "is_scanned": True}


def _cancel_build(monkeypatch, calls):
    async def _boom(*_a, **_k):
        calls.append(1)
        raise asyncio.CancelledError()

    monkeypatch.setattr(source, "_build", _boom)


def test_cancelled_build_writes_negative_cache_and_reraises(monkeypatch, tmp_path):
    """取消：memo 必须写入（reason_code=cancelled、TTL 有限），CancelledError 上抛。"""
    source.reset_memo()
    calls: list[int] = []
    _cancel_build(monkeypatch, calls)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            source.boxes_for_page(
                file_id="c" * 8,
                snapshot=_snapshot(tmp_path),
                page=1,
                ocr_has_types=None,
            )
        )

    assert calls == [1], "首次请求必须真的进建链（否则测的是假路径）"
    memo = source._MEMO.get(next(iter(source._MEMO))) if source._MEMO else None
    assert memo is not None, "取消后必须落 memo（缺陷本体：此前一个字都不写）"
    assert memo.get("unusable") is True
    assert memo.get("reason_code") == "cancelled"
    retry_at = float(memo.get("retry_at"))
    assert retry_at != float("inf"), "取消是暂态：TTL 必须有限，过期后允许重建"


def test_retry_within_ttl_short_circuits_without_rebuild(monkeypatch, tmp_path):
    """TTL 内重试：直接命中负缓存短路，不再重放 O(整卷) 建链（无限复现的根）。"""
    source.reset_memo()
    calls: list[int] = []
    _cancel_build(monkeypatch, calls)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            source.boxes_for_page(
                file_id="d" * 8,
                snapshot=_snapshot(tmp_path),
                page=1,
                ocr_has_types=None,
            )
        )

    # 重试：不再触发 _build，负缓存直接短路返回（带机读原因，#165 口径）
    boxes, memo = asyncio.run(
        source.boxes_for_page(
            file_id="d" * 8,
            snapshot=_snapshot(tmp_path),
            page=1,
            ocr_has_types=None,
        )
    )
    assert calls == [1], "重试不得再进建链——否则失败→重试→再失败循环仍在"
    assert boxes is None
    assert memo is not None and memo.get("unusable") is True
    assert memo.get("reason_code") == "cancelled"


def test_retry_after_ttl_rebuilds(monkeypatch, tmp_path):
    """TTL 过期后允许重建（暂态故障可能已恢复），与既有负缓存口径一致。"""
    source.reset_memo()
    calls: list[int] = []
    _cancel_build(monkeypatch, calls)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            source.boxes_for_page(
                file_id="e" * 8,
                snapshot=_snapshot(tmp_path),
                page=1,
                ocr_has_types=None,
            )
        )

    # 把已存负缓存的 retry_at 拨到过去 ⇒ 下次 _lookup_memo 判过期，重建资格恢复
    # （TTL 在首建时已折算进 retry_at，改 settings 不影响已存 marker）
    key = next(iter(source._MEMO))
    source._MEMO[key]["retry_at"] = __import__("time").monotonic() - 1.0
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            source.boxes_for_page(
                file_id="e" * 8,
                snapshot=_snapshot(tmp_path),
                page=1,
                ocr_has_types=None,
            )
        )
    assert len(calls) == 2, "TTL 过期后必须允许重建"


def test_settings_default_ttl_is_finite():
    """口径钉：取消负缓存必须有有限 TTL（走 settings 可调）。"""
    ttl = getattr(settings, "TEXT_LAYER_DOCLINE_CANCEL_NEGATIVE_TTL_SEC", None)
    assert ttl is not None, "TTL 必须是显式 settings 项（可观测、可调）"
    assert 0 < float(ttl) < float("inf")
