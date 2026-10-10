"""本地抽取式 NER 引擎（Issue #90 POC 阶段 0）：GLiNER / UIE 零样本接入 T2 考场。

引擎协议与 benchmark_t2 一致：predict(text, types) -> dict[类型, list[实体串]]。
抽取式性质守卫：span 必须在原文中子串可命中，未命中 span 记入 warnings（不静默丢弃、
也不进预测——保证"画记号不写字"可校验）。torch/paddle 依赖全部延迟导入，
两个引擎不同进程使用（torch-DCU 与 paddle 混进程不可行）。
"""
import time


class LazyEngine:
    """惰性代理：argparse 阶段只记参数，首次属性访问才加载模型——
    保证 --buckets 校验失败时零加载成本，torch/paddle 也只在真正参赛时导入。"""

    def __init__(self, factory, arg):
        self._factory, self._arg, self._real = factory, arg, None

    def _ensure(self):
        if self._real is None:
            self._real = self._factory(self._arg)

    def __getattr__(self, attr):
        self._ensure()
        return getattr(self._real, attr)


def materialize(engine):
    """参数校验后统一物化：返回真实引擎对象（避免代理 setattr 影子化 warnings 等可变属性）。"""
    if isinstance(engine, LazyEngine):
        engine._ensure()
        return engine._real
    return engine


class GlinerEngine:
    """GLiNER 零样本：类型作为自然语言 label 直传。"""

    def __init__(self, model: str, threshold: float = 0.5, device: str | None = None):
        self.name = f"gliner({model})"
        self.model_name = model
        self.threshold = threshold
        self.warnings: list[str] = []
        import torch
        from gliner import GLiNER  # 延迟到构造，跑 has 引擎时零依赖
        resolved = device or ("cuda" if torch.cuda.is_available() else "cpu")
        t0 = time.perf_counter()
        self.model = GLiNER.from_pretrained(model).to(resolved)
        self.device = resolved
        self.load_sec = time.perf_counter() - t0

    async def predict(self, text: str, types: list[str]) -> dict[str, list[str]]:
        spans = self.model.predict_entities(text, labels=list(types),
                                            threshold=self.threshold)
        out: dict[str, list[str]] = {}
        for sp in spans:
            span_text, label = sp["text"], sp["label"]
            if span_text not in text:
                self.warnings.append(f"non-substring span dropped: {span_text!r} ({label})")
                continue
            out.setdefault(label, []).append(span_text)
        return out


class UieEngine:
    """PaddleNLP UIE 零样本：schema=类型中文名直传，taskflow 抽取。"""

    def __init__(self, model: str, device: str = "cpu"):
        self.name = f"uie({model})"
        self.model_name = model
        self.warnings: list[str] = []
        from paddlenlp import Taskflow  # 延迟导入，避免 torch 进程污染
        t0 = time.perf_counter()
        self.ie = Taskflow("information_extraction", schema=["占位"], model=model,
                           device=device)
        self.device = device
        self.load_sec = time.perf_counter() - t0

    async def predict(self, text: str, types: list[str]) -> dict[str, list[str]]:
        self.ie.set_schema(types)
        try:
            results = self.ie(text)
        except Exception as exc:
            self.warnings.append(f"uie predict error: {exc}")
            return {}
        out: dict[str, list[str]] = {}
        for item in results:  # taskflow 对单条输入返回 [ {类型: [ {text,...} ]} ]
            for etype, spans in (item or {}).items():
                for sp in spans or []:
                    span_text = sp.get("text", "")
                    if not span_text:
                        continue
                    if span_text not in text:
                        self.warnings.append(f"non-substring span dropped: {span_text!r} ({etype})")
                        continue
                    out.setdefault(etype, []).append(span_text)
        return out
