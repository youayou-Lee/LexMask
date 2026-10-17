"""
OCR 服务客户端
通过 HTTP 调用独立的 PaddleOCR-VL 微服务 (端口8082)
不再在后端进程中加载 PaddleOCR 模型
"""
from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

from app.core.circuit_breaker import CircuitBreaker
from app.core.config import settings
from app.core.health_checks import _tcp_port_open
from app.core.retry import RETRYABLE_HTTPX, retry_sync
from app.services import model_config_service


class OCRServiceError(Exception):
    """OCR 服务错误，区分瞬态（可重试）和永久错误。"""
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class OCRItem:
    text: str
    x: float      # 归一化坐标 0-1
    y: float
    width: float
    height: float
    confidence: float
    label: str = "text"
    chars: list = field(default_factory=list)  # per-char boxes (normalized) L→R


class OCRService:
    """OCR 服务客户端 - 通过 HTTP 调用独立微服务（连接池复用 + 多端点轮询/故障回落，Issue#169）"""

    # failover 后故障端点的冷却期（秒）：期间轮询跳过它，到期自动回到轮换
    _FAILOVER_COOLDOWN_S = 15.0

    def __init__(self) -> None:
        self._read_timeout = float(settings.OCR_TIMEOUT)
        # 健康检查用短超时客户端（复用连接池）
        probe_timeout = max(5.0, float(settings.OCR_HEALTH_PROBE_TIMEOUT))
        self._health_client = httpx.Client(timeout=probe_timeout, trust_env=False)
        # OCR 推理用长超时客户端
        ocr_timeout = httpx.Timeout(connect=15.0, read=self._read_timeout, write=60.0, pool=15.0)
        self._ocr_client = httpx.Client(timeout=ocr_timeout, trust_env=False)
        self._health_checked_at = 0.0
        self._health_ready = False
        self._health_base_url = ""
        # 多端点池（Issue#169）：轮询计数 + 故障端点冷却表 + 每端点独立熔断器
        # （门①.5 C1：共享熔断器会让 A 端故障把健康 B 端一并拦掉，故 per-endpoint）
        self._endpoint_lock = threading.Lock()
        self._rr = 0
        self._down_until: dict[str, float] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._health_cache: dict[str, tuple[float, bool]] = {}

    @staticmethod
    def _is_live_health_payload(data: dict) -> bool:
        status = str(data.get("status", "")).lower()
        if status in {"busy", "loading"}:
            return True
        if status in {"unavailable", "degraded"}:
            return False
        return bool(data["ready"]) if "ready" in data else True

    @property
    def base_url(self) -> str:
        return model_config_service.get_paddle_ocr_base_url()

    def _extra_endpoints(self) -> list[str]:
        """`OCR_ENDPOINTS` 附加端点（去空、去重、排除主端点）；空配置 ⇒ []（单实例旧行为）"""
        extras: list[str] = []
        for raw in (settings.OCR_ENDPOINTS or "").split(","):
            url = raw.strip().rstrip("/")
            if url and url != self.base_url and url not in extras:
                extras.append(url)
        return extras

    def _candidate_endpoints(self) -> list[str]:
        """全部端点按轮询次序返回，冷却中的过滤；全部冷却中 ⇒ 清冷却、返回全部（对齐旧单实例行为）。"""
        endpoints = [self.base_url] + self._extra_endpoints()
        if len(endpoints) == 1:
            return endpoints
        with self._endpoint_lock:
            self._rr += 1
            cut = self._rr % len(endpoints)
            ordered = endpoints[cut:] + endpoints[:cut]
        now = time.monotonic()
        live = [u for u in ordered if self._down_until.get(u, 0.0) <= now]
        if live:
            return live
        with self._endpoint_lock:
            self._down_until.clear()
        return ordered

    def _mark_endpoint(self, url: str, ok: bool) -> None:
        with self._endpoint_lock:
            if ok:
                self._down_until.pop(url, None)
            else:
                self._down_until[url] = time.monotonic() + self._FAILOVER_COOLDOWN_S

    def _breaker_for(self, url: str) -> CircuitBreaker:
        with self._endpoint_lock:
            breaker = self._breakers.get(url)
            if breaker is None:
                breaker = CircuitBreaker(name=f"ocr[{url}]", threshold=5, reset_timeout=30.0)
                self._breakers[url] = breaker
            return breaker

    def _post_with_failover(self, path: str, json_payload: dict) -> httpx.Response:
        """POST 到轮询选出的 OCR 端点；连接失败/超时/5xx/熔断 OPEN 依次回落下一端点
        （失败端点进冷却）；4xx 原样返回（上层状态语义有效，如 /structure 404 = 在线但缺路由）。"""
        last_exc: Exception | None = None
        last_resp: httpx.Response | None = None
        for url in self._candidate_endpoints():
            try:
                resp = self._breaker_for(url).call_sync(
                    lambda u=url: self._ocr_client.post(f"{u}{path}", json=json_payload)
                )
            except RuntimeError as e:
                # 熔断器对该端点 OPEN ⇒ 视同该端点故障，回落下一端点（各端点熔断器独立）
                logger.warning("OCR endpoint %s%s breaker open (%s); failing over", url, path, e)
                last_exc = e
                self._mark_endpoint(url, ok=False)
                continue
            except (httpx.ConnectError, httpx.NetworkError, httpx.TimeoutException) as e:
                logger.warning("OCR endpoint %s%s unreachable (%s); failing over", url, path, e)
                last_exc = e
                self._mark_endpoint(url, ok=False)
                continue
            if resp.status_code >= 500:
                logger.warning(
                    "OCR endpoint %s%s returned HTTP %d; failing over", url, path, resp.status_code
                )
                last_resp = resp
                self._mark_endpoint(url, ok=False)
                continue
            self._mark_endpoint(url, ok=True)
            return resp
        if last_resp is not None:
            return last_resp
        assert last_exc is not None
        raise last_exc

    def is_available(self) -> bool:
        """检查 OCR 微服务是否在线且模型已就绪（多实例时任一实例就绪即可用）"""
        now = time.monotonic()
        base_url = self.base_url
        if not self._extra_endpoints():
            # 单实例：原行为不变（5s TTL，主端点）
            if self._health_base_url == base_url and now - self._health_checked_at < 5.0:
                return self._health_ready
            ready = self._probe_ready(base_url)
            self._health_ready = ready
            self._health_checked_at = now
            self._health_base_url = base_url
            return ready
        # 多实例：任一端点就绪即可用（逐端点 5s TTL）
        for url in [base_url] + self._extra_endpoints():
            cached = self._health_cache.get(url)
            if cached and now - cached[0] < 5.0:
                if cached[1]:
                    return True
                continue
            ready = self._probe_ready(url)
            self._health_cache[url] = (now, ready)
            if ready:
                return True
        return False

    def _probe_ready(self, url: str) -> bool:
        try:
            resp = self._health_client.get(f"{url}/health")
            if resp.status_code == 200:
                return self._is_live_health_payload(resp.json())
        except httpx.TimeoutException:
            if _tcp_port_open(f"{url}/health"):
                return True
        except Exception as e:
            logger.debug("OCR health check failed (%s): %s", url, e)
        return False

    def get_model_name(self) -> str:
        """获取模型名称"""
        try:
            resp = self._health_client.get(f"{self.base_url}/health")
            if resp.status_code == 200:
                return resp.json().get("model", "PaddleOCR-VL")
        except Exception as e:
            logger.debug("OCR model name check failed: %s", e)
        return "PaddleOCR-VL"

    def _do_ocr_request(self, image_b64: str) -> httpx.Response:
        """Execute a single OCR HTTP request (retryable, pooled client, endpoint failover)."""
        return self._post_with_failover(
            "/ocr",
            {"image": image_b64, "max_new_tokens": settings.OCR_MAX_NEW_TOKENS},
        )

    def _do_structure_request(self, image_b64: str) -> httpx.Response:
        """Execute a PP-StructureV3 request through the OCR microservice (endpoint failover)."""
        return self._post_with_failover("/structure", {"image": image_b64})

    def _raise_for_bad_status(self, label: str, resp: httpx.Response) -> None:
        status = int(resp.status_code)
        body = getattr(resp, "text", "")[:200]
        transient = status >= 500 or status in {408, 409, 425, 429}
        msg = f"{label} service returned HTTP {status}: {body}"
        logger.error(msg)
        raise OCRServiceError(msg, transient=transient)

    def extract_text_boxes(self, image_bytes: bytes) -> list[OCRItem]:
        """调用 OCR 微服务提取文本框"""
        if not image_bytes:
            return []

        if settings.OCR_REQUIRE_GPU and not self.is_available():
            raise OCRServiceError(
                "OCR 服务离线且已启用 OCR_REQUIRE_GPU，拒绝 CPU 回退。"
                "请启动 GPU 服务: docker compose --profile gpu up -d",
                transient=False,
            )

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        try:
            # 连接要快失败；读取等待 VL 完整推理（默认数分钟）
            resp = retry_sync(
                self._do_ocr_request, image_b64,
                max_retries=2, base_delay=1.0,
                retryable_exceptions=RETRYABLE_HTTPX,
            )
            if resp.status_code != 200:
                self._raise_for_bad_status("OCR", resp)

            data = resp.json()
            items = []
            for box in data.get("boxes", []):
                items.append(OCRItem(
                    text=box["text"],
                    x=box["x"],
                    y=box["y"],
                    width=box["width"],
                    height=box["height"],
                    confidence=box.get("confidence"),
                    label=box.get("label", "text"),
                ))
            logger.info("OCR Client got %d boxes in %.2fs", len(items), data.get('elapsed', 0))
            return items

        except OCRServiceError:
            raise
        except httpx.TimeoutException as e:
            msg = (
                f"OCR 微服务超时（read≈{self._read_timeout:.0f}s）。"
                "若 OCR 微服务仍在推理属正常，请增大 OCR_TIMEOUT；若长期无响应请查看 ocr_server 控制台。"
            )
            logger.warning(msg)
            raise OCRServiceError(msg, transient=True) from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            msg = f"无法连接 OCR 微服务 ({self.base_url}): {e}"
            logger.error(msg)
            raise OCRServiceError(msg, transient=True) from e
        except Exception as e:
            msg = f"OCR 识别失败: {e}"
            logger.error(msg)
            raise OCRServiceError(msg, transient=False) from e

    def extract_structure_boxes(self, image_bytes: bytes) -> list[OCRItem]:
        """Call PP-StructureV3 table/layout OCR and return text boxes."""
        if not image_bytes:
            return []

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        try:
            resp = retry_sync(
                self._do_structure_request, image_b64,
                max_retries=1, base_delay=1.0,
                retryable_exceptions=RETRYABLE_HTTPX,
            )
            if resp.status_code == 404:
                logger.info("OCR structure endpoint unavailable; skipping PP-StructureV3")
                return []
            if resp.status_code != 200:
                self._raise_for_bad_status("OCR Structure", resp)

            data = resp.json()
            items = []
            for box in data.get("boxes", []):
                items.append(OCRItem(
                    text=box["text"],
                    x=box["x"],
                    y=box["y"],
                    width=box["width"],
                    height=box["height"],
                    confidence=box.get("confidence"),
                    label=box.get("label", "structure"),
                    chars=box.get("chars", []) or [],
                ))
            logger.info("OCR Structure client got %d boxes in %.2fs", len(items), data.get("elapsed", 0))
            return items
        except OCRServiceError:
            raise
        except httpx.TimeoutException as e:
            msg = f"PP-StructureV3 OCR timed out after {self._read_timeout:.0f}s"
            logger.warning(msg)
            raise OCRServiceError(msg, transient=True) from e
        except (httpx.ConnectError, httpx.NetworkError) as e:
            msg = f"Cannot connect to OCR structure service ({self.base_url}): {e}"
            logger.error(msg)
            raise OCRServiceError(msg, transient=True) from e
        except Exception as e:
            msg = f"PP-StructureV3 OCR failed: {e}"
            logger.error(msg)
            raise OCRServiceError(msg, transient=False) from e


# 全局实例
ocr_service = OCRService()
