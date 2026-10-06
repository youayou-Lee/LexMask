# backend/tests/test_benchmark_t2_ingest.py
import json
import sys
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval" / "benchmarks" / "t2"))
import ingest_hardcase as ing  # noqa: E402


@pytest.fixture(scope="module")
def sample_pdf(tmp_path_factory):
    import fitz
    p = tmp_path_factory.mktemp("hc") / "doc.pdf"
    d = fitz.open()
    d.new_page().insert_text((72, 72), "被告人张三身份证号11010119600101000X案号(2023)粤01刑终100号", fontname="china-s")
    d.save(p); d.close()
    return p


def test_build_entry_ok(sample_pdf):
    e = ing.build_hardcase_entry(sample_pdf, 0, [("姓名", "张三")], story="姓名漏检", origin="issue#53")
    assert e["bucket"] == "hardcase" and e["bucket_kind"] == "hardcase"
    assert e["entities"]["姓名"] == ["张三"]
    assert e["origin"] == "issue#53" and "张三" in e["text"]
    assert ing.ENTRY_SCHEMA_KEYS <= set(e)


def test_reject_entity_not_in_text(sample_pdf):
    with pytest.raises(ValueError, match="不在文本中"):
        ing.build_hardcase_entry(sample_pdf, 0, [("姓名", "不存在的人")], story="x", origin="t")


def test_reject_unknown_type(sample_pdf):
    with pytest.raises(ValueError, match="不在 preset"):
        ing.build_hardcase_entry(sample_pdf, 0, [("外星类型", "张三")], story="x", origin="t")


def test_raw_form_substitutes(tmp_path_factory):
    # GT 串（规范形态）与文中形态不同时，raw_forms 命中即收
    import fitz
    p = tmp_path_factory.mktemp("hc2") / "doc.pdf"
    d = fitz.open()
    d.new_page().insert_text((72, 72), "身份证号 110101196001010 00X 备案", fontname="china-s")
    d.save(p); d.close()
    e = ing.build_hardcase_entry(
        p, 0, [("身份证号", "11010119600101000X")],
        story="空格打断", origin="t", raw_forms={"11010119600101000X": "110101196001010 00X"},
    )
    assert e["entities"]["身份证号"] == ["11010119600101000X"]
    assert e["raw_forms"]["11010119600101000X"] == "110101196001010 00X"


def test_reject_bad_id_format(sample_pdf):
    with pytest.raises(ValueError, match="身份证"):
        ing.build_hardcase_entry(sample_pdf, 0, [("身份证号", "123")], story="x", origin="t",
                                 raw_forms={"123": "11010119600101000X"})


def test_cli_writes_jsonl_and_manifest(sample_pdf, tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "hardcase"
    rc = ing.main([
        "--file", str(sample_pdf), "--page", "0",
        "--entity", "姓名:张三",
        "--story", "姓名漏检", "--origin", "issue#53",
        "--out-dir", str(out_dir),
    ])
    assert rc == 0
    jsonl = out_dir / "hardcase.jsonl"
    assert jsonl.exists()
    lines = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1 and lines[0]["entities"]["姓名"] == ["张三"]
    manifest = tmp_path / "manifest.private.json"
    assert manifest.exists()
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert m["entries"][0]["id"] == lines[0]["id"]
    assert m["entries"][0]["origin"] == "issue#53"
    out = capsys.readouterr().out
    assert "hardcase" in out and "issue#53" in out
    # 追加第二条
    ing.main(["--file", str(sample_pdf), "--page", "0", "--entity", "姓名:张三",
              "--story", "y", "--origin", "t2", "--out-dir", str(out_dir)])
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 2
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(m["entries"]) == 2


def test_text_file_mode_uses_transcript_as_text(sample_pdf, tmp_path):
    # 扫描件无文字层：OCR 转写文件作为条目 text，实体校验以转写为准（PDF 只做页码溯源）
    transcript = tmp_path / "p3.txt"
    transcript.write_text("受案登记表 案号：粤公粤(交警)受案字(2023)00680号 车牌粤R12345", encoding="utf-8")
    e = ing.build_hardcase_entry(
        sample_pdf, 0, [("案号", "粤公粤(交警)受案字(2023)00680号"), ("车牌号", "粤R12345")],
        story="案号整串漏检", origin="issue#51",
        text_file=transcript,
    )
    assert e["text"] == transcript.read_text(encoding="utf-8")
    assert e["entities"]["案号"] == ["粤公粤(交警)受案字(2023)00680号"]


def test_text_file_mode_rejects_entity_absent_from_transcript(sample_pdf, tmp_path):
    transcript = tmp_path / "p3.txt"
    transcript.write_text("转写里没有这个实体", encoding="utf-8")
    with pytest.raises(ValueError, match="不在文本中"):
        ing.build_hardcase_entry(sample_pdf, 0, [("姓名", "张三")], story="x", origin="t",
                                 text_file=transcript)


def test_text_file_mode_still_validates_page(sample_pdf, tmp_path):
    transcript = tmp_path / "p9.txt"
    transcript.write_text("随便", encoding="utf-8")
    with pytest.raises(ValueError, match="越界"):
        ing.build_hardcase_entry(sample_pdf, 9, [("姓名", "张三")], story="x", origin="t",
                                 text_file=transcript)


def test_source_ref_and_verify_stored_in_entry_and_manifest(sample_pdf, tmp_path):
    e = ing.build_hardcase_entry(sample_pdf, 0, [("姓名", "张三")], story="x", origin="t",
                                 source_ref="testdata/eval37-real/real_zqc_wenshu_p1-5.pdf#p3",
                                 verify="dual-ai-agree")
    assert e["source_ref"] == "testdata/eval37-real/real_zqc_wenshu_p1-5.pdf#p3"
    assert e["verify"] == "dual-ai-agree"
    out_dir = tmp_path / "hardcase"
    e["id"] = ing._next_id(out_dir)
    ing._write_entry(e, out_dir)
    m = json.loads((out_dir.parent / "manifest.private.json").read_text(encoding="utf-8"))
    assert m["entries"][0]["source_ref"] == e["source_ref"]
    assert m["entries"][0]["verify"] == "dual-ai-agree"


def test_cli_passes_text_file_source_ref_verify(sample_pdf, tmp_path, capsys):
    out_dir = tmp_path / "hardcase"
    transcript = tmp_path / "p1.txt"
    transcript.write_text("被告人张三", encoding="utf-8")
    rc = ing.main([
        "--file", str(sample_pdf), "--page", "0",
        "--text-file", str(transcript),
        "--entity", "姓名:张三",
        "--story", "x", "--origin", "issue#51",
        "--source-ref", "testdata/x.pdf#p1", "--verify", "adjudicated",
        "--out-dir", str(out_dir),
    ])
    assert rc == 0
    lines = [json.loads(l) for l in (out_dir / "hardcase.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines[0]["text"] == "被告人张三"
    assert lines[0]["source_ref"] == "testdata/x.pdf#p1" and lines[0]["verify"] == "adjudicated"


def test_multi_fragment_raw_form(sample_pdf, tmp_path):
    # 跨行/跨框碎片：raw_forms 支持 ｜ 分隔多片段，逐片段命中即收；任一片段缺失拒收
    transcript = tmp_path / "p1.txt"
    transcript.write_text("公安局道\n交通警察大队\n落款", encoding="utf-8")
    e = ing.build_hardcase_entry(
        sample_pdf, 0, [("机关单位", "清远市公安局交通警察大队")],
        story="跨行机构名", origin="t",
        text_file=transcript,
        raw_forms={"清远市公安局交通警察大队": "公安局道｜交通警察大队"},
    )
    assert e["raw_forms"]["清远市公安局交通警察大队"] == "公安局道｜交通警察大队"
    with pytest.raises(ValueError, match="不在文本中"):
        ing.build_hardcase_entry(
            sample_pdf, 0, [("机关单位", "某单位")], story="x", origin="t",
            text_file=transcript,
            raw_forms={"某单位": "公安局道｜缺失片段"},
        )


def test_multiple_entities_same_type(sample_pdf):
    # 同类型多实体（一页多人名/多日期是真实案卷常态）：不得互相覆盖
    e = ing.build_hardcase_entry(sample_pdf, 0,
                                 [("姓名", "张三"), ("姓名", "李四"), ("案号", "(2023)粤01刑终100号")],
                                 story="多人名", origin="t")
    assert e["entities"]["姓名"] == ["张三", "李四"]
    assert e["entities"]["案号"] == ["(2023)粤01刑终100号"]


def test_cli_reject_no_partial_write(sample_pdf, tmp_path):
    out_dir = tmp_path / "hardcase"
    rc = ing.main([
        "--file", str(sample_pdf), "--page", "0",
        "--entity", "姓名:张三", "--entity", "姓名:不存在",
        "--story", "x", "--origin", "t", "--out-dir", str(out_dir),
    ])
    assert rc != 0
    assert not (out_dir / "hardcase.jsonl").exists()
    assert not (tmp_path / "manifest.private.json").exists()

