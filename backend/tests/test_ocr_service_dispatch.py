"""Issue#169：OCRService 多端点池（轮询 / 故障回落 / 冷却 / 熔断共存）单元测试。

全部离线：HTTP 层用 fake client 替身，不触网、不依赖 GPU。覆盖：
- 单端点缺省（OCR_ENDPOINTS 空）⇒ 行为与旧版一致（回归锚）
- 双端点轮询、连接失败/5xx 回落、冷却与回归、全灭报错
- 4xx 不回落（/structure 404 = 在线但缺路由的既有语义）
- 熔断 OPEN 视同端点故障回落（per-endpoint breaker）
- is_available 多实例任一就绪即可用
"""
from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.services import ocr_service as ocr_service_module
from app.services.ocr_service import OCRService, OCRServiceError

PRIMARY = "http://127.0.0.1:8082"
SECONDARY = "http://127.0.0.1:18092"


class FakeClient:
    """按 URL 分派的假 httpx 客户端。behavior: fn(url, json) -> httpx.Response（或抛异常）。"""

    def __init__(self, behavior):
        self.calls: list[str] = []
        self._behavior = behavior

    def post(self, url, json=None):
        self.calls.append(url)
        return self._behavior(url, json)

    def get(self, url):  # 健康探针用
        return self._behavior(url, None)


class PassthroughBreaker:
    """替身熔断器：直通。可设 open_urls 使引用了这些端点的调用抛 RuntimeError（模拟熔断 OPEN）。

    _post_with_failover 的调用形如 lambda u=url: ...（端点在默认参数里），据此识别。
    """

    def __init__(self, open_urls=()):
        self.open_urls = set(open_urls)

    def call_sync(self, func, *args, **kwargs):
        for d in func.__defaults__ or ():
            if any(d.startswith(o) for o in self.open_urls):
                raise RuntimeError(f"服务 [ocr] 暂时不可用: {d}")
        return func(*args, **kwargs)


def bases(fake):
    """记录的完整 URL 剥掉路径，还原成端点 base 便于断言。"""
    return [c.rsplit("/", 1)[0] for c in fake.calls]


def make_service(monkeypatch, endpoints="", require_gpu=False):
    monkeypatch.setattr(settings, "OCR_ENDPOINTS", endpoints, raising=False)
    monkeypatch.setattr(settings, "OCR_REQUIRE_GPU", require_gpu, raising=False)
    monkeypatch.setattr(
        ocr_service_module.model_config_service,
        "get_paddle_ocr_base_url",
        lambda: PRIMARY,
    )
    return OCRService()


def ok(body=None):
    return httpx.Response(200, json=body if body is not None else {"boxes": [], "elapsed": 0})


def img(image_b64="x"):
    return image_b64


def test_single_endpoint_default_no_rotation(monkeypatch):
    """OCR_ENDPOINTS 空 ⇒ 只有主端点，行为与旧版一致（回归锚）。"""
    svc = make_service(monkeypatch)
    fake = FakeClient(lambda url, json: ok({"boxes": [], "elapsed": 0}))
    svc._ocr_client = fake
    for _ in range(3):
        resp = svc._do_ocr_request(img())
        assert resp.status_code == 200
    assert bases(fake) == [PRIMARY] * 3


def test_rotation_two_endpoints(monkeypatch):
    """双端点健康 ⇒ 轮询交替。"""
    svc = make_service(monkeypatch, endpoints=f"{PRIMARY},{SECONDARY}")
    fake = FakeClient(lambda url, json: ok())
    svc._ocr_client = fake
    for _ in range(4):
        svc._do_ocr_request(img())
    assert bases(fake).count(PRIMARY) == 2
    assert bases(fake).count(SECONDARY) == 2
    assert bases(fake)[0] != bases(fake)[1]


def test_failover_on_connect_error_and_cooldown(monkeypatch):
    """主端点始终拒连：轮询撞上即回落另一端（始终 200）；冷却期内不再选它；清冷却后重新被轮询到。"""
    svc = make_service(monkeypatch, endpoints=f"{PRIMARY},{SECONDARY}")

    def behavior(url, json):
        if url.startswith(PRIMARY):
            raise httpx.ConnectError("connection refused")
        return ok()

    fake = FakeClient(behavior)
    svc._ocr_client = fake
    for _ in range(4):
        resp = svc._do_ocr_request(img())
        assert resp.status_code == 200
    assert bases(fake).count(PRIMARY) == 1  # 轮询首撞（拒绝→回落），冷却期内不再选它
    assert bases(fake)[-1] == SECONDARY
    svc._down_until.clear()
    for _ in range(4):
        svc._do_ocr_request(img())
    assert PRIMARY in bases(fake)  # 冷却到期 ⇒ 重新参与轮换（拒→回落，响应仍 200）


def test_failover_on_5xx(monkeypatch):
    """主端点 5xx ⇒ 回落第二端点。"""
    svc = make_service(monkeypatch, endpoints=f"{PRIMARY},{SECONDARY}")

    def behavior(url, json):
        if url.startswith(PRIMARY):
            return httpx.Response(500, json={"detail": "boom"})
        return ok()

    svc._ocr_client = FakeClient(behavior)
    resp = svc._do_ocr_request(img())
    assert resp.status_code == 200


def test_all_down_raises_transient(monkeypatch):
    """全部端点连接失败 ⇒ 异常抛出（外层包装为 OCRServiceError transient）。"""
    svc = make_service(monkeypatch, endpoints=f"{PRIMARY},{SECONDARY}")
    svc._ocr_client = FakeClient(
        lambda url, json: (_ for _ in ()).throw(httpx.ConnectError("refused"))
    )
    with pytest.raises(httpx.ConnectError):
        svc._do_ocr_request(img())
    with pytest.raises(OCRServiceError) as ei:
        svc.extract_text_boxes(b"x")
    assert ei.value.transient is True


def test_4xx_no_failover(monkeypatch):
    """/structure 4xx = 端点在线但语义有效：原样返回，不得回落（404 = 缺路由跳过的既有语义）。"""
    svc = make_service(monkeypatch, endpoints=f"{PRIMARY},{SECONDARY}")
    fake = FakeClient(lambda url, json: httpx.Response(404, json={"detail": "not found"}))
    svc._ocr_client = fake
    resp = svc._do_structure_request(img())
    assert resp.status_code == 404
    assert len(bases(fake)) == 1  # 4xx 不触发第二端点


def test_breaker_open_fails_over(monkeypatch):
    """熔断 OPEN 视同端点故障回落（per-endpoint breaker，门①.5 C1）。"""
    svc = make_service(monkeypatch, endpoints=f"{PRIMARY},{SECONDARY}")
    svc._ocr_client = FakeClient(lambda url, json: ok())
    svc._breakers[PRIMARY] = PassthroughBreaker(open_urls={PRIMARY})
    for _ in range(3):
        resp = svc._do_ocr_request(img())
        assert resp.status_code == 200
    assert PRIMARY not in bases(svc._ocr_client)


def test_is_available_any_endpoint(monkeypatch):
    """多实例时任一端点就绪即可用（门①.5：is_available 单值缓存需随双端点改造）。"""
    svc = make_service(monkeypatch, endpoints=f"{PRIMARY},{SECONDARY}")

    def behavior(url, json=None):
        if url.startswith(PRIMARY):
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"status": "online", "ready": True})

    svc._health_client = FakeClient(behavior)
    assert svc.is_available() is True


def test_is_available_single_endpoint_unchanged(monkeypatch):
    """单实例 is_available 行为不变（回归锚）：不就绪 ⇒ False。"""
    svc = make_service(monkeypatch)
    svc._health_client = FakeClient(
        lambda url, json=None: httpx.Response(200, json={"status": "online", "ready": False})
    )
    assert svc.is_available() is False
