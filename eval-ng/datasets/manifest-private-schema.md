# 真卷清单契约（`manifest.private.json`）— 公开说明

> **真卷本体、真卷清单、真卷报告一律不入公开仓**（AGENTS.md 铁律 1）。本文件只描述**形状**，
> 供 `eval-ng` 的 `speed` / `run-e2e` 两条线一致地读取；示例文件见 `eval/datasets/manifest.private.example.json`。

## 1. 文件位置与来源

| 项 | 约定 |
|---|---|
| 清单路径 | `eval/datasets/manifest.private.json`（**不提交**；`.gitignore` 覆盖） |
| 生成方式 | `python eval/scripts/build_real_set.py --src <案卷源目录> --out <私有数据目录>` |
| 页数截取 | 生成器按 `p1-15` 这类片段命名，`pages` 必须与真实页数一致 |
| 各环境 | 本地与云实例**各自生成**：路径是绝对路径，跨机器不可复用 |

## 2. 条目字段（`files[]`）

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `id` | str | ✓ | 全局唯一，建议 `real_<案卷>_<描述>_p<a>-<b>` |
| `path` | str | ✓ | **绝对路径**；文件须在私有体系内 |
| `pages` | int | ✓ | 真实页数；speed 线**据此判定轮次合法性**（缺省或 ≤0 直接报错） |
| `gt` | null/obj | ✓ | 真实卷恒为 `null`（无 GT ⇒ `perf_only`，只记耗时） |
| `source` | str | ✓ | `real`；合成卷为 `synthetic`（`--suite` 与它对齐，防真卷冒充合成卷） |
| `carrier` | str | ✓ | `scanned_pdf` / `text_pdf` / `docx` / `txt`；speed 线用 `pdftotext` 现场复核 PDF 文本层 |
| `doc_type` | str | – | 业务类别（不影响判据） |
| `density` | str | – | 文本密度标签，可填 `n/a` |
| `levels` | [str] | ✓ | 含 `e2e` 才会被 `make_exam_from_manifest.py` 收进考卷 |
| `access` | str | ✓ | `private` |
| `why` / `notes` | str | – | 选样理由（私有归档用，**不**进报告） |
| `generator` | str | – | 生成命令留痕，便于复现 |
| `expect_reject` | bool | – | 声明「该卷应被后端拒绝」（加密/损坏卷）；speed 线据此走 D6③ 判定 |

## 3. 从清单到 speed 考卷

```bash
python eval-ng/scripts/make_exam_from_manifest.py \
  --repo-root <本仓绝对路径> --suite real --out /tmp/exam_real_speed.json
python eval-ng/cli.py speed /tmp/exam_real_speed.json \
  --driver single --arm <私有臂 JSON> --out <私有目录>/speed-real
```

考卷形状（`make_exam_from_manifest.py` 产出，或手写）：

```json
{
  "suite": "real",
  "corpus": [
    {"id": "real_caseA_contract_p1-67", "path": "/private/abs/xxx.pdf", "pages": 67,
     "carrier": "scanned_pdf", "md5": "<现场 md5>", "expect_md5": "<锚点 md5>"}
  ]
}
```

- `suite` 必须显式声明：`--suite real` 只接受 `"suite":"real"` 的考卷，反之亦然（双向防错配）。
- `pages` **必填**；`carrier` 建议填（缺省时 PDF 用文本层现场推断）。
- `md5`/`expect_md5` 二选一或都给：给 `expect_md5` 即成为锚点校验，卷内容漂移会直接报错（防「换了卷还说 2.28×」）。
- 真卷条目**不得**携带任何质量字段（P/R/F1 等）；报告导出时由 `report.py` 的白名单/黑名单覆盖再查一遍。

## 4. 报告落盘与留存

- 真卷报告**只落私有目录**（`LexMask-workspace/` 下），文件名带 `real`，跑完由人工/脚本搬运；
- 出私有体系前必须过脱敏扫描器（D5④）：

```bash
python eval-ng/scripts/check_report_redaction.py <报告.json> <报告.md> \
  --denylist <一行一词的禁词表> --allow-prefix <允许出现的私有绝对路径前缀>
```

- 扫描器退出码：`0` 干净 / `1` 命中 / `2` 用法错误；**必须**先跑 `--self-test`（内置阳性对照）证明它抓得住。
- `.json` 报告不经 `leak_check.py`（它面向 `docs/` Markdown + 姓名映射 CSV，遇 `.json` 抛 `ValueError`）。