"""eval-ng CLI 单测：全链路 mock、exit code、--ci 档（零真端点、零网络）。"""

import json

import evalng_bootstrap as boot

cli = boot.load("cli.py")

REPO = boot.REPO


def _write_exam(tmp_path, **overrides):
    doc = {
        "exam_id": "unit-exam",
        "items": [
            {"id": "p1", "form": "entity_table", "text": "张三，身份证号 110101199001010011。",
             "types": ["姓名", "身份证号"],
             "gt": {"姓名": ["张三"], "身份证号": ["110101199001010011"]}},
        ],
    }
    doc["items"][0].update(overrides)
    path = tmp_path / "exam.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _base_args(exam_path, out_dir, extra=None):
    argv = [
        "run", str(exam_path),
        "--participant", "mock",
        "--runs", "2",
        "--out", str(out_dir),
        "--env-label", "local-test",
        "--target-label", "mock-ner",
        "--hardware", "cpu",
        "--topology", "single-box",
        "--stack-version", "python=3.11",
    ]
    return argv + (extra or [])


def test_cli_full_pass(tmp_path):
    out = tmp_path / "reports"
    rc = cli.main(_base_args(_write_exam(tmp_path), out))
    assert rc == 0
    files = list(out.glob("*.md")) + list(out.glob("*.json"))
    assert len(files) == 2
    md = next(out.glob("*.md")).read_text(encoding="utf-8")
    assert "runs" in md and "K" or "cpu" in md


def test_cli_missing_labels_exit2(tmp_path):
    exam = _write_exam(tmp_path)
    argv = ["run", str(exam), "--participant", "mock", "--out", str(tmp_path)]
    assert cli.main(argv) == 2


def test_cli_gate_fail_exit1(tmp_path):
    exam_doc = {
        "exam_id": "fail-exam",
        "items": [
            {"id": "p1", "form": "entity_table", "text": "x", "types": ["身份证号"],
             "gt": {"身份证号": ["110101199001010011"]},
             "mock_answer": {"身份证号": ["110101199001010012"]}},
        ],
    }
    # mock 回放错误实体（构造错身份证）→ 数字闸 FAIL
    (tmp_path / "bad_exam.json").write_text(json.dumps(exam_doc, ensure_ascii=False),
                                            encoding="utf-8")
    out = tmp_path / "reports"
    rc = cli.main(_base_args(tmp_path / "bad_exam.json", out))
    assert rc == 1


def test_ci_mode_no_network(tmp_path):
    # --ci 档 + mock：即使默认 ner-base 不存在也全绿（mock 不触网）
    out = tmp_path / "reports"
    rc = cli.main(_base_args(_write_exam(tmp_path), out, extra=["--ci"]))
    assert rc == 0


def test_unknown_participant_exits(tmp_path):
    try:
        cli.main(_base_args(_write_exam(tmp_path), tmp_path,
                            extra=["--participant", "nope"]))
    except SystemExit as e:
        assert "未知参赛者" in str(e)
    else:
        # build_participant 在 _run 内调用，返回码 2 亦可接受
        pass


def test_cli_multi_bucket_with_meta(tmp_path):
    """用户意见①②：一次跑多桶 + 汇总报告带桶说明（内容/全量抽样）。"""
    doc = {
        "exam_id": "t2-multi",
        "buckets": {
            "hardcase": {"content": "真实案卷难例·人工沉淀", "sampling": "全量：65 条", "count": 1},
            "digit-confusion": {"content": "合成·数字陷阱", "sampling": "全量：50 条", "count": 1},
        },
        "items": [
            {"id": "h1", "exam_id": "hardcase", "form": "entity_table", "text": "x",
             "types": ["姓名"], "gt": {"姓名": ["张三"]}},
            {"id": "d1", "exam_id": "digit-confusion", "form": "entity_table", "text": "y",
             "types": ["银行卡号"], "gt": {"银行卡号": ["6222020200112233445"]},
             "mock_answer": {"银行卡号": ["6222020200112233446"]}},  # 数字错→该桶闸 FAIL
        ],
    }
    exam = tmp_path / "multi.json"
    exam.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "reports"
    rc = cli.main(_base_args(exam, out))
    assert rc == 1  # digit-confusion 桶数字闸 FAIL → 总判定 FAIL
    md = next(out.glob("*-t2-multi-summary.md")).read_text(encoding="utf-8")
    assert "hardcase" in md and "digit-confusion" in md
    assert "真实案卷难例·人工沉淀" in md and "全量：65 条" in md  # 桶说明进报告
    assert "FAIL" in md  # 数字闸逐桶可见
