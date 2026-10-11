"""GT 工作台 HTTP 服务（Issue#56 M3 / Task 3）。

启动器（终审修复波 I4）::

    python3 -m gt.workbench_server --work ~/gt-work [--pdf-root PATH] [--port N]

（在 eval/ 目录下运行，或 PYTHONPATH 含 eval；host 固定 127.0.0.1 本机回环，
端口默认 8600。这是用户与门禁⑤在真实 ``--work`` 目录上启动本工具的方式。）

FastAPI 薄封装：端点 = ``workbench.py`` 数据层函数的直通包装 + 少量状态
（``pdf_root``，供 ``/img/{page_id}`` 定位源 PDF）。零网络（TestClient 离线
自测）、不碰任何 git 仓（铁律 1：GT 原文不进仓）。

控制器裁定：

- 400：数据层 ``ValueError`` 原文（中文信息）放 ``{"error": ...}``；
- 409：finalize 前置不满足 → ``{"missing": [...]}`` 逐项中文缺项清单；
- ``/api/undo``：journal 空 → 400（数据层 ValueError 透传）；
- ``GET /api/page/{page_id}``：pack JSON + ``image_url: /img/{page_id}``
  （客户端对 404 回落转录视图）；页不存在 404；
- ``GET /img/{page_id}``：按 pack ``source.file_sha256`` 在 ``pdf_root`` 下
  定位 ``{sha256}.pdf``，经 ``render.render_page_png`` 渲入缓存目录
  （``{pdf_root or work}/render_cache``）；pdf_root 未设 / 文件缺失 / 渲染器
  不可用一律 404 + ``{"error": 原因}``。

依赖注记：fastapi/uvicorn 由 backend/requirements.txt 携带（不新增依赖）；
pypdfium2 为可选依赖（缺失时 /img 降级 404，前端回落转录高亮）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "eval") not in sys.path:
    sys.path.insert(0, str(_REPO / "eval"))

from gt import gt_schema, render, workbench  # noqa: E402

_STATIC = Path(__file__).resolve().parent / "workbench_static"

_DEFAULT_PORT = 8600


def create_app(work: Path, pdf_root: Path | None = None) -> FastAPI:
    """构造工作台应用；``work`` = 数据层工作目录，``pdf_root`` = 源 PDF 根。"""
    work = Path(work)
    app = FastAPI(title="LexMask GT 工作台", version="0.1.0")
    state = {"pdf_root": Path(pdf_root) if pdf_root is not None else None}

    def _run(fn, *args, **kwargs):
        """数据层直通：ValueError（中文错误信息）→ 400 {"error": ...}。

        返回 JSONResponse 时直接透传（负路径），否则返回数据层结果。
        """
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})

    def _404(reason: str):
        return JSONResponse(status_code=404, content={"error": reason})

    # ---- 只读端点 -----------------------------------------------------------

    @app.get("/api/stats")
    def stats():
        return workbench.load_workspace(work).stats()

    @app.get("/api/disputes")
    def disputes():
        return workbench.load_workspace(work).disputes()

    @app.get("/api/journal")
    def journal(n: int = 20):
        # M4 终审修复：n 钳到 >= 0（负值在 [-n:] 切片下语义漂移）
        return workbench.journal_tail(work, max(0, n))

    @app.get("/api/trust")
    def trust():
        # Task 5：前端 boot 即拉可信率——无抽样时 404 + 中文原因（同 /api/sample
        # 口径），而非让 trust_rate 的 ValueError 冒泡成 500。
        if not (work / "sample_seed.json").is_file():
            return _404("尚无抽样（先在抽检视图点「开始抽检」）")
        return workbench.trust_rate(work)

    @app.get("/api/preset-types")
    def preset_types():
        """preset 实体类型名集（Task 5 前端改判/补漏表单下拉数据源）。

        单一事实源直出（gt_schema.PRESET_TYPE_NAMES ← preset_entity_types.json），
        前端不硬编码类型清单，preset 改名自动跟随。
        """
        return sorted(gt_schema.PRESET_TYPE_NAMES)

    @app.get("/api/sample")
    def get_sample():
        """抽样清单只读（Task 5 前端抽检视图契约）：无抽样 → 404 + 中文原因。"""
        path = work / "sample_seed.json"
        if not path.is_file():
            return _404("尚无抽样（先在抽检视图点「开始抽检」）")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/page/{page_id}")
    def page(page_id: str):
        try:
            pack = workbench._read_pack(work, page_id)
        except ValueError as exc:
            return _404(str(exc))
        return {**pack, "image_url": f"/img/{page_id}"}

    # ---- 写端点（ValueError → 400 {"error": 中文信息}） -----------------------

    @app.post("/api/resolve")
    def resolve(body: dict):
        # 终审修复波 C1：透传显式 adjudication_index（前端恒传；缺省 = None 时
        # 数据层走旧 span/text 关联兜底，兼容旧 journal/API 用户）
        return _run(workbench.resolve_dispute,
                    work, body.get("page_id"), body.get("entity_index"),
                    body.get("verdict"), body.get("correct"), body.get("note"),
                    body.get("adjudication_index"))

    @app.post("/api/undo")
    def undo():
        return _run(workbench.undo_last, work)

    @app.post("/api/sample")
    def sample(body: dict | None = None):
        body = body or {}
        return _run(workbench.make_sample, work, body.get("ratio", 0.1),
                    body.get("seed"))

    @app.post("/api/sample-verdict")
    def sample_verdict(body: dict):
        return _run(workbench.apply_sample_verdict,
                    work, body.get("page_id"), body.get("entity_index"),
                    bool(body.get("ok")), body.get("correct"))

    # ---- finalize（前置检查 → 409 {"missing": 缺项清单}） ----------------------

    def _selected_count() -> int:
        data = json.loads((work / "sample_seed.json").read_text(encoding="utf-8"))
        return len(data.get("selected", []))

    @app.post("/api/finalize")
    def finalize():
        missing: list[str] = []
        ws = workbench.load_workspace(work)
        n_disputed = sum(len(v) for v in ws.disputes().values())
        if n_disputed:
            missing.append(f"未裁决 {n_disputed} 条")
        if not (work / "sample_seed.json").is_file():
            missing.append("抽检未完成（尚无抽样，先在抽检视图点「开始抽检」）")
        else:
            # I1 终审修复（硬门禁）：确认+改判须覆盖全部抽样——旧口径用
            # checked（含未复审的 consistent 条目）比对总数，门禁恒空转。
            trust = workbench.trust_rate(work)
            total = _selected_count()
            reviewed = trust["confirmed"] + trust["corrected"]
            if reviewed < total:
                missing.append(f"抽检未完成（未复审 {total - reviewed} 条"
                               f" / 共 {total} 条）")
        if missing:
            return JSONResponse(status_code=409, content={"missing": missing})
        try:
            gt_schema.write_gt_jsonl(list(ws.pages.values()), work / "gt_v1.jsonl", "v1")
        except ValueError as exc:  # 未被触过的页定稿前才首次校验：非法 → 400 非 500
            return JSONResponse(status_code=400, content={"error": str(exc)})
        return {"ok": True, "out": str(work / "gt_v1.jsonl"), "pages": len(ws.pages)}

    # ---- 页面渲染（Task 4 可选依赖；任何不可用 → 404 {"error": 原因}） ---------

    @app.get("/img/{page_id}")
    def img(page_id: str):
        if state["pdf_root"] is None:
            return _404("未配置 --pdf-root，无法渲染页面图")
        try:
            source = workbench._read_pack(work, page_id)["source"]
        except (ValueError, KeyError) as exc:
            return _404(f"页 {page_id!r} 缺 source：{exc}")
        sha = source.get("file_sha256", "")
        page_no = source.get("page", 0)
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return _404(f"source.file_sha256 {sha!r} 非 64 位小写十六进制")
        pdf = state["pdf_root"] / f"{sha}.pdf"
        if not pdf.is_file():
            return _404(f"源 PDF 缺失（{pdf}）")
        cache_dir = state["pdf_root"] / "render_cache"
        out = cache_dir / render.png_name(pdf, page_no)
        if not out.is_file() and not render.render_page_png(str(pdf), page_no, out):
            return _404("页面渲染不可用（pypdfium2 缺失或渲染失败）")
        return FileResponse(out, media_type="image/png")

    # ---- 静态首页 ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        index_html = _STATIC / "index.html"
        if not index_html.is_file():
            return _404("前端未就绪（缺 index.html）")
        return HTMLResponse(index_html.read_text(encoding="utf-8"))

    return app


def _main(argv: list[str] | None = None) -> int:
    """启动器（I4 终审修复）：``python3 -m gt.workbench_server --work ~/gt-work``。

    host 固定 127.0.0.1（本机回环，工作台不对外网暴露）；``--work`` 必填，
    ``--pdf-root`` 缺省不渲染页面图（前端回落转录高亮）。
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m gt.workbench_server",
        description="GT 工作台服务（Issue#56 M3）——在真实引擎工作目录上启动")
    parser.add_argument("--work", required=True,
                        help="工作目录（{work}/pages/*/pack.json 布局，M1 引擎产出）")
    parser.add_argument("--pdf-root", default=None,
                        help="源 PDF 根目录（按 {file_sha256}.pdf 定位；缺省不渲染页面图）")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT,
                        help=f"监听端口（默认 {_DEFAULT_PORT}）")
    args = parser.parse_args(argv)

    import uvicorn

    work = Path(args.work).expanduser()
    pdf_root = Path(args.pdf_root).expanduser() if args.pdf_root else None
    app = create_app(work, pdf_root)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
