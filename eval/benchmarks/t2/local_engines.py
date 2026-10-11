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


class UieDynamicEngine:
    """UIE 动态图推理（Issue#90 阶段1）：直接加载训练 checkpoint（UIEM dygraph），
    绕开 paddlenlp2.8.1 taskflow 与 paddle3.x 静态导出格式不兼容问题。
    解码逻辑忠实移植 taskflow：prompt+content 编码 → start/end 概率 →
    get_bool_ids_greater_than/get_span/get_id_and_prob → span；长文按句切窗
    （taskflow _auto_splitter 的同思路简化：句号等边界切、pack 到 max_predict_len）。"""

    def __init__(self, model_path: str, device: str | None = None,
                 position_prob: float = 0.5, max_seq_len: int = 512, batch_size: int = 32):
        self.name = f"uie-dynamic({model_path})"
        self.model_name = model_path
        self.position_prob = position_prob
        self.max_seq_len = max_seq_len
        self.batch_size = batch_size
        self.warnings: list[str] = []
        import numpy as np
        import paddle
        from paddlenlp.transformers import UIEM, AutoTokenizer
        from paddlenlp.utils.tools import get_bool_ids_greater_than, get_span
        from paddlenlp.taskflow.utils import get_id_and_prob
        self._np, self._gb, self._gs, self._gip = np, get_bool_ids_greater_than, get_span, get_id_and_prob
        resolved = device or ("gpu" if (paddle.device.is_compiled_with_cuda()
                                        and paddle.device.cuda.device_count() > 0) else "cpu")
        paddle.set_device(resolved)
        t0 = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.model = UIEM.from_pretrained(model_path)
        self.model.eval()
        self.device = resolved
        self.load_sec = time.perf_counter() - t0

    def _split(self, text: str, max_predict_len: int) -> list[str]:
        if len(text) <= max_predict_len:
            return [text]
        # 句界切分后 pack（与 taskflow _auto_splitter 同思路：句内不切断）
        import re
        sents = re.findall(r"[^。！？；\n]+[。！？；\n]?", text)
        chunks, buf = [], ""
        for s in sents:
            if len(buf) + len(s) > max_predict_len and buf:
                chunks.append(buf)
                buf = s if len(s) <= max_predict_len else ""
                if not buf:  # 超长单句硬切
                    for k in range(0, len(s), max_predict_len):
                        chunks.append(s[k:k + max_predict_len])
            else:
                buf += s
        if buf:
            chunks.append(buf)
        return chunks

    async def predict(self, text: str, types: list[str]) -> dict[str, list[str]]:
        np = self._np
        out: dict[str, list[str]] = {}
        for prompt in types:
            max_predict_len = self.max_seq_len - len(prompt) - 3  # [CLS] prompt [SEP] text [SEP]
            segs = self._split(text, max_predict_len)
            spans: list[tuple[int, int]] = []
            for bstart in range(0, len(segs), self.batch_size):
                batch = segs[bstart:bstart + self.batch_size]
                encoded = self.tokenizer(
                    text=[prompt] * len(batch), text_pair=batch,
                    truncation=True, max_seq_len=self.max_seq_len,
                    pad_to_max_seq_len=True, return_attention_mask=False,
                    return_position_ids=True, return_offsets_mapping=True, return_dict=False)
                input_ids = np.array([e["input_ids"] for e in encoded], dtype="int64")
                pos_ids = np.array([e["position_ids"] for e in encoded], dtype="int64")
                offset_maps = [np.array(e["offset_mapping"]) for e in encoded]
                import paddle
                with paddle.no_grad():
                    tids = paddle.to_tensor(input_ids)
                    pids = paddle.to_tensor(pos_ids)
                    start_prob, end_prob = self.model(input_ids=tids, position_ids=pids)
                s_ids = self._gb(start_prob.numpy().tolist(), limit=self.position_prob, return_prob=True)
                e_ids = self._gb(end_prob.numpy().tolist(), limit=self.position_prob, return_prob=True)
                for si, (s_list, e_list, omap) in enumerate(
                        zip(s_ids, e_ids, offset_maps)):
                    seg_offset = sum(len(s) for s in segs[:bstart + si])
                    span_set = self._gs(s_list, e_list, with_prob=True)
                    ids, _probs = self._gip(span_set, omap.tolist())
                    for st, en in ids:
                        a, b = st + seg_offset, en + seg_offset
                        spans.append((a, b))
            seen = set()
            vals = []
            for a, b in sorted(spans):
                v = text[a:b]
                if not v or v in seen:
                    continue
                seen.add(v)
                vals.append(v)
            for v in vals:
                if v not in text:  # 理论不可能，子串守卫兜底
                    self.warnings.append(f"non-substring span dropped: {v!r} ({prompt})")
                    continue
                out.setdefault(prompt, []).append(v)
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
