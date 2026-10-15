"""eval-ng manifest v2 单测：合并、hash 校验、类型映射收敛。"""

import hashlib
import json
from pathlib import Path

import evalng_bootstrap as boot

mv = boot.load("datasets/manifest_v2.py")


def _write_exam(tmp: Path, content: bytes) -> Path:
    p = tmp / "exam1.txt"
    p.write_bytes(content)
    return p


def _make_manifest(tmp: Path, sha: str) -> Path:
    doc = {
        "version": 2,
        "files": [
            {
                "id": "e1",
                "exam_id": "hardcase",
                "form": "entity_table",
                "path": "exam1.txt",
                "content_sha256": sha,
                "gt": {"姓名": ["张三"]},
            }
        ],
    }
    p = tmp / "manifest.v2.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_merge_public_private(tmp_path):
    content = "张某于2026年1月1日签署。".encode()
    exam = _write_exam(tmp_path, content)
    sha = hashlib.sha256(content).hexdigest()
    pub = _make_manifest(tmp_path, sha)
    priv = tmp_path / "manifest.private.json"
    priv.write_text(
        json.dumps(
            {"files": [{"id": "e1", "exam_id": "hardcase",
                        "form": "entity_table",
                        "resolved_path": str(exam)}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    items = mv.load_manifest_v2(pub, priv, base_dir=tmp_path)
    assert len(items) == 1
    assert items[0].id == "e1"
    assert items[0].form == mv.AnswerForm.ENTITY_TABLE
    assert items[0].gt == {"姓名": ["张三"]}
    assert items[0].payload["path"] == str(exam)


def test_hash_mismatch_raises(tmp_path):
    _write_exam(tmp_path, b"real content")
    pub = _make_manifest(tmp_path, hashlib.sha256(b"other").hexdigest())
    try:
        mv.load_manifest_v2(pub, None, base_dir=tmp_path)
    except mv.ManifestMismatch:
        pass
    else:
        raise AssertionError("expected ManifestMismatch")


def test_hash_match_passes(tmp_path):
    content = b"byte identical"
    _write_exam(tmp_path, content)
    pub = _make_manifest(tmp_path, hashlib.sha256(content).hexdigest())
    items = mv.load_manifest_v2(pub, None, base_dir=tmp_path)
    assert items[0].payload["path"].endswith("exam1.txt")


def test_no_sha_no_check(tmp_path):
    doc = {
        "version": 2,
        "files": [{"id": "e1", "exam_id": "x", "form": "entity_table", "path": "exam1.txt",
                   "gt": None}],
    }
    (tmp_path / "manifest.v2.json").write_text(json.dumps(doc), encoding="utf-8")
    _write_exam(tmp_path, b"whatever")
    items = mv.load_manifest_v2(tmp_path / "manifest.v2.json", None, base_dir=tmp_path)
    assert items[0].gt is None  # gt=None → perf_only 语义由 runner 处理


def test_unknown_form_raises(tmp_path):
    doc = {"version": 2, "files": [{"id": "e1", "exam_id": "x", "form": "video", "path": "a"}]}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    try:
        mv.load_manifest_v2(p, None, base_dir=tmp_path)
    except mv.ManifestError as e:
        assert "form" in str(e)
    else:
        raise AssertionError("expected ManifestError")


def test_robustness_expect_reject_default(tmp_path):
    doc = {"version": 2, "files": [{"id": "r1", "exam_id": "x", "form": "robustness",
                                    "path": "enc.pdf"}]}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    items = mv.load_manifest_v2(p, None, base_dir=tmp_path)
    assert items[0].payload["expect_reject"] is True


def test_type_maps_converged():
    tm = mv.load_type_maps()
    assert tm["cluener"]["name"] == "姓名"
    assert tm["leven"] == {}
    assert tm["resume"]["NAME"] == "姓名"
