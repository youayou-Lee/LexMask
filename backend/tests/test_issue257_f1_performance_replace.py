"""Issue#257 一期 · AC2.2：页账合并的「整表/整页替换」语义（F1 前置缺陷）。

本会话真 ``JobStore`` 实证（修复前必红）：

* ``update {"recognition":{"failed_pages":{"2":"timeout"}}}`` 后再写
  ``{"recognition":{"failed_pages":{}}}`` → 读回仍是 ``{"2":"timeout"}``
  （``#199`` 注释宣称的「空 dict 清旧键」在深合并下不成立）；
* 页 5 先写 ``{"status":"failed","error":"boom","stage_duration_ms":{...}}``
  再写 ``{"status":"completed","duration_ms":10}`` → 读回仍带 ``error`` 与
  ``stage_duration_ms``（单页重试成功后页格「成功但带错误」、交付闸继续读失败清单）。

判据只钉**可观测行为**（AC2.2），不钉参数名之外的实现细节；同时钉住默认路径
（不带替换集）仍逐字节沿用深合并，避免顺手改坏既有调用点。
"""

from __future__ import annotations

import pytest

from app.services.job_store import JobItemStatus, JobStore, JobType


def _make_item(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    jid = store.create_job(job_type=JobType.TEXT_BATCH, title="t", owner_id="admin")
    iid = store.add_item(jid, "file-1")
    store.update_item_status(iid, JobItemStatus.AWAITING_REVIEW)
    return store, jid, iid


def _replace_paths(*paths: tuple[str, ...]) -> set[tuple[str, ...]]:
    return set(paths)


def test_empty_failed_pages_clears_old_entries(tmp_path):
    """AC2.2 ①：识别→页失败→重试成功后，失败清单必须清空。"""
    store, _jid, iid = _make_item(tmp_path)
    store.update_item_performance(iid, {"recognition": {"failed_pages": {"2": "timeout"}}})
    store.update_item_performance(
        iid,
        {"recognition": {"failed_pages": {}}},
        replace_paths=_replace_paths(("recognition", "failed_pages")),
    )
    assert store.get_item_performance(iid)["recognition"]["failed_pages"] == {}


def test_failed_pages_partial_update_keeps_other_keys(tmp_path):
    """消账某页不得连带清掉其它失败页（红→绿是逐页语义）。"""
    store, _jid, iid = _make_item(tmp_path)
    store.update_item_performance(
        iid, {"recognition": {"failed_pages": {"2": "timeout", "3": "ner boom"}}}
    )
    store.update_item_performance(
        iid,
        {"recognition": {"failed_pages": {"3": "ner boom"}}},
        replace_paths=_replace_paths(("recognition", "failed_pages")),
    )
    assert store.get_item_performance(iid)["recognition"]["failed_pages"] == {"3": "ner boom"}


def test_page_record_replace_drops_stale_error_fields(tmp_path):
    """AC2.2 ②：同一页二次写入 completed 后，旧 error/stage_duration_ms 必须消失。"""
    store, _jid, iid = _make_item(tmp_path)
    store.update_item_performance(
        iid,
        {
            "recognition": {
                "pages": {
                    "5": {"status": "failed", "error": "boom", "stage_duration_ms": {"x": 1}}
                }
            }
        },
    )
    store.update_item_performance(
        iid,
        {"recognition": {"pages": {"5": {"status": "completed", "duration_ms": 10}}}},
        replace_paths=_replace_paths(("recognition", "pages", "5")),
    )
    page = store.get_item_performance(iid)["recognition"]["pages"]["5"]
    assert page == {"status": "completed", "duration_ms": 10}


def test_page_record_replace_does_not_touch_other_pages(tmp_path):
    """只替换目标页：其它页的记录逐字节不变（AC2.1 的其余页不变判据的底座）。"""
    store, _jid, iid = _make_item(tmp_path)
    store.update_item_performance(
        iid,
        {"recognition": {"pages": {"4": {"status": "completed", "duration_ms": 7}}}},
    )
    before = store.get_item_performance(iid)["recognition"]["pages"]["4"]
    store.update_item_performance(
        iid,
        {"recognition": {"pages": {"5": {"status": "completed", "duration_ms": 9}}}},
        replace_paths=_replace_paths(("recognition", "pages", "5")),
    )
    assert store.get_item_performance(iid)["recognition"]["pages"]["4"] == before


def test_default_merge_semantics_unchanged(tmp_path):
    """AC2.2 ③：不带替换集的既有调用点必须逐字节沿用深合并（含「空 dict 不删键」）。"""
    store, _jid, iid = _make_item(tmp_path)
    store.update_item_performance(
        iid,
        {"recognition": {"page_count": 6, "pages": {"1": {"status": "completed"}}}},
    )
    store.update_item_performance(iid, {"recognition": {"pages": {}}})
    perf = store.get_item_performance(iid)
    assert perf["recognition"]["page_count"] == 6
    assert perf["recognition"]["pages"] == {"1": {"status": "completed"}}


def test_replace_path_missing_target_creates_it(tmp_path):
    """目标键原本不存在时替换语义=直接写入（重试第一页的常见形态）。"""
    store, _jid, iid = _make_item(tmp_path)
    store.update_item_performance(
        iid,
        {"recognition": {"pages": {"7": {"status": "completed", "duration_ms": 3}}}},
        replace_paths=_replace_paths(("recognition", "pages", "7")),
    )
    assert store.get_item_performance(iid)["recognition"]["pages"]["7"] == {
        "status": "completed",
        "duration_ms": 3,
    }


def test_unknown_item_still_raises_key_error(tmp_path):
    store, _jid, _iid = _make_item(tmp_path)
    with pytest.raises(KeyError):
        store.update_item_performance(
            "no-such-item",
            {"recognition": {"failed_pages": {}}},
            replace_paths=_replace_paths(("recognition", "failed_pages")),
        )
