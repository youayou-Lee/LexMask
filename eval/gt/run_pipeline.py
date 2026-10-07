"""GT 预标流水线 CLI：单页模式与 synthetic 批量模式（Issue#56 M1 / Task 7）。

只做参数解析与循环，全部装配逻辑在 ``gt.pagepack.run_page``（CLI 薄壳约定）。

单页模式::

    python eval/gt/run_pipeline.py \
        --sample 页面副本.pdf --page 0 --page-type body \
        --clients "cloud:PP-OCRv6,cloud:PaddleOCR-VL-1.6[,vlmd:http://127.0.0.1:8095]" \
        --ner off | --ner-base URL \
        --work GT工作目录 [--carrier scanned] [--segment first]

批量模式（synthetic manifest，格式见 eval/datasets/manifest.json）::

    python eval/gt/run_pipeline.py --suite synthetic --manifest eval/datasets/manifest.json \
        --clients ... --ner off --work GT工作目录

批量语义：
- 遍历 manifest ``files``，逐文件逐页（``pages`` 字段，缺省 1）调 run_page；
- **levels 过滤**（T7 评审 rider）：``levels`` 不含 ``"e2e"`` 的条目一律跳过
  （现 manifest 的 ``ner_corpus_10p`` levels=["ner"] 是 NER 引擎层语料，
  JSONL 不是可转录原件，绝不能送云）；
- 页型映射：edge 样本 → ``edge``（edge 旗标 / id 含 edge / generator 含
  edge=True 三种写法都认，兼容现 manifest 把 edge 藏在 generator 串里的现状）；
  ``doc_type == "bank_statement"`` → ``table``；其余 → ``body``；
- carrier 逐条目透传（缺省 "scanned"），segment 用 ``--segment``（缺省 "first"）；
- NER 设置对全部页生效；
- **逐页失败不中断批次**：失败页记 stderr 后继续，结束时输出「总/成功/失败」
  对账；存在失败页则以非零码退出（页数对账是 E 闸口径，失败必须显性化）；
- 同一文件多页共用一次转录结果（pagepack.CachedTranscriptionClient 包一层，
  防止逐页重复打云作业——多页文件一次云作业返回全部页）。

NER：``--ner-base URL`` 启用 HTTP NER 通道（pagepack.HTTPNERClient，端点形状
待 T8 与 backend 实测对齐）；``--ner off`` 或缺省 = 关闭（零网络缺省）。

云凭据：token 只经环境变量 CLOUD_VL_TOKEN（gt.unify.build_clients 负责，
本模块零接触凭据）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # 直接脚本执行（python eval/gt/run_pipeline.py）：补 eval/ 进 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gt.entities import NEROff  # noqa: E402
from gt.gt_schema import PAGE_TYPES  # noqa: E402
from gt.lock import acquire_work_lock, release_work_lock  # noqa: E402
from gt.pagepack import (  # noqa: E402
    OpenAINERClient,
    CachedTranscriptionClient,
    HTTPNERClient,
    run_page,
)
from gt.unify import CloudVLClient, LocalVLClient, build_clients  # noqa: E402

# 双云通道的模型名 → clients dict 键（a = 云 v6、b = 云 VL；md = 本地 vl-md）
# legacy "PaddleOCR-VL" 在 CloudVLClient 构造时已翻译为 "PaddleOCR-VL-1.6"，此处键用翻译后的串
_CLOUD_MODEL_KEYS = {"PP-OCRv6": "a", "PaddleOCR-VL-1.6": "b"}


# ---- 参数解析辅助 ----------------------------------------------------------------

def parse_clients(spec: str) -> dict[str, object]:
    """把逗号分隔的转录客户端 spec 组装成 run_page 的 clients dict。

    ``cloud:PP-OCRv6`` → ``"a"``、``cloud:PaddleOCR-VL-1.6`` → ``"b"``、
    ``vlmd:<base_url>`` → ``"md"``（可选，顺序不敏感）。a/b 双云缺一即
    ``ValueError``（双云互验是管线前提，不允许单云降级）。
    """
    clients: dict[str, object] = {}
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        client = build_clients(piece)
        if isinstance(client, CloudVLClient):
            key = _CLOUD_MODEL_KEYS.get(client.model)
            if key is None:
                raise ValueError(
                    f"未知云模型 {client.model!r}（支持 {'/'.join(_CLOUD_MODEL_KEYS)}）")
            clients[key] = client
        elif isinstance(client, LocalVLClient):
            clients["md"] = client
        else:
            raise ValueError(f"未知转录客户端类型: {type(client).__name__}（spec: {piece!r}）")
    missing = {"a", "b"} - set(clients)
    if missing:
        raise ValueError(
            f"--clients 缺少双云通道 {sorted(missing)}：需要 cloud:PP-OCRv6 与 "
            "cloud:PaddleOCR-VL-1.6（vlmd 可选；legacy 串 cloud:PaddleOCR-VL 亦可，"
            "构造时自动翻译为 1.6）")
    return clients


def build_ner(ner_base: str | None, ner_flag: str | None,
              ner_shape: str = "openai", ner_model: str | None = None):
    """NER 参数 → NERClient：``--ner-base URL`` → OpenAINERClient（openai，实测真实形状）
    或 HTTPNERClient（entities，直连 REST 假定形状）；off/缺省 → NEROff。"""
    if ner_base:
        if ner_shape == "entities":
            return HTTPNERClient(ner_base)
        return OpenAINERClient(ner_base, model=ner_model)
    return NEROff()


def map_page_type(entry: dict) -> str:
    """manifest 条目 → GT 页型：edge 样本 → edge；bank_statement → table；余 body。

    edge 判定兼容三种写法：显式 ``edge`` 旗标、id 含 "edge"、generator 串含
    ``edge=True``（现 manifest 的边界样本把 edge 藏在 generator 里）。
    """
    if (entry.get("edge") or "edge" in str(entry.get("id", ""))
            or "edge=True" in str(entry.get("generator", ""))):
        return "edge"
    if entry.get("doc_type") == "bank_statement":
        return "table"
    return "body"


# ---- 两种运行模式 ----------------------------------------------------------------

def _run_single(args: argparse.Namespace, clients: dict, ner) -> int:
    pack = run_page(args.sample, args.page, args.page_type, clients, ner,
                    Path(args.work), carrier=args.carrier, segment=args.segment)
    n_disputed = sum(1 for adj in pack["adjudications"] if adj["verdict"] == "disputed")
    print(f"[run] {pack['page_id']}: 实体 {len(pack['entities'])} 条，"
          f"待人工裁决 {n_disputed} 条 → "
          f"{Path(args.work) / 'pages' / pack['page_id'] / 'pack.json'}")
    return 0


def _run_batch(args: argparse.Namespace, clients: dict, ner) -> int:
    # 单实例守卫（2026-10-07 实战缺陷：并发跑批在同一 work 目录竞态）；单页模式不加锁
    work = Path(args.work)
    acquire_work_lock(work)
    try:
        return _run_batch_unlocked(args, clients, ner)
    finally:
        release_work_lock(work)


def _run_batch_unlocked(args: argparse.Namespace, clients: dict, ner) -> int:
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("files") or []
    entries = [e for e in entries if "e2e" in (e.get("levels") or [])]  # T7 评审 rider：非 e2e 条目不送云
    base_dir = manifest_path.parent
    work = Path(args.work)
    total = ok = failed = 0
    failures: list[str] = []
    for entry in entries:
        fpath = base_dir / str(entry.get("path", ""))
        page_type = map_page_type(entry)
        carrier = str(entry.get("carrier") or "scanned")
        n_pages = int(entry.get("pages") or 1)
        # 每个文件包一层转录缓存：多页文件的全部页共用一次转录结果
        wrapped = {key: CachedTranscriptionClient(c) for key, c in clients.items()}
        for page_no in range(n_pages):
            total += 1
            label = f"{entry.get('id', fpath.name)}-p{page_no:03d}"
            try:
                run_page(str(fpath), page_no, page_type, wrapped, ner, work,
                         carrier=carrier, segment=args.segment)
                ok += 1
            except Exception as e:  # 逐页失败隔离：记档、继续批次
                failed += 1
                failures.append(f"{label}: {type(e).__name__}: {e}")
                print(f"[batch] 页失败 {label}: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"[batch] 完成：总 {total} 页 / 成功 {ok} / 失败 {failed}", file=sys.stderr)
    for line in failures:
        print(f"[batch]   失败明细 {line}", file=sys.stderr)
    return 1 if failed else 0  # 失败页必须显性化（页数对账口径）


# ---- CLI -------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_pipeline",
        description="GT 预标流水线：双云互验 + 逐页 pagepack（Issue#56 M1）")
    parser.add_argument("--sample", help="单页模式：样本文件路径")
    parser.add_argument("--page", type=int, default=0, help="页号（默认 0）")
    parser.add_argument("--page-type", default="body", choices=sorted(PAGE_TYPES),
                        help="页型（默认 body）")
    parser.add_argument("--clients", default="cloud:PP-OCRv6,cloud:PaddleOCR-VL-1.6",
                        help="转录客户端 spec，逗号分隔：cloud:PP-OCRv6,cloud:PaddleOCR-VL-1.6"
                             "[,vlmd:URL]（双云必选，vlmd 可选）")
    parser.add_argument("--ner-shape", choices=["openai", "entities"], default="openai",
                        help="NER 端点形状：openai=vLLM /chat/completions（实测真实形状，默认）；entities=直连 REST 假定形状")
    parser.add_argument("--ner-model", default=None, help="NER 模型名（vLLM 单模型可省）")
    ner_group = parser.add_mutually_exclusive_group()
    ner_group.add_argument("--ner-base", default=None,
                           help="NER 端点 URL（启用 NER 通道；端点形状待 T8 对齐）")
    ner_group.add_argument("--ner", choices=["off"], default=None,
                           help="--ner off 关闭 NER 通道（缺省即关闭）")
    parser.add_argument("--work", required=True, help="GT 工作目录（pack 落盘根）")
    parser.add_argument("--carrier", default="scanned", help="原件载体（默认 scanned）")
    parser.add_argument("--segment", default="first", help="卷内段位（默认 first）")
    parser.add_argument("--suite", choices=["synthetic"], default=None,
                        help="批量模式：synthetic（遍历 manifest 合成集）")
    parser.add_argument("--manifest", default=None, help="批量模式：manifest.json 路径")
    args = parser.parse_args(argv)
    if args.suite:
        if not args.manifest:
            parser.error("--suite synthetic 需要 --manifest PATH")
        if args.sample:
            parser.error("--sample 与 --suite 互斥（单页/批量二选一）")
    elif not args.sample:
        parser.error("单页模式需要 --sample PATH（或 --suite synthetic 批量模式）")
    return args


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：返回进程退出码（0 成功；批量模式存在失败页为 1）。"""
    args = _parse_args(argv)
    try:
        clients = parse_clients(args.clients)
        ner = build_ner(args.ner_base, args.ner, ner_shape=args.ner_shape, ner_model=args.ner_model)
        if args.suite:
            return _run_batch(args, clients, ner)
        return _run_single(args, clients, ner)
    except Exception as e:  # CLI 边界：错误进 stderr、非零退出（批量内部已逐页隔离）
        print(f"[run_pipeline] 错误: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
