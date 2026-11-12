from app.core.config import settings


def test_issue273_routing_defaults():
    assert settings.RECOGNITION_ROUTING_MODE == "page"
    assert settings.ROUTING_VERSION == 1
    assert settings.ROUTING_CHARS_MULTIPLE == 1.3
    assert settings.ROUTING_ENTITIES_MULTIPLE == 1.5
    assert settings.ROUTING_VERIFY_UPGRADE_PASS == 3
    assert settings.ROUTING_SAMPLE_PAGES == 2
    assert settings.ROUTING_MAX_PAGE_REPEAT_RATIO == 0.30
    assert settings.ROUTING_MIN_READING_ORDER_AGREEMENT == 0.80
    assert settings.ROUTING_BOUNDARY_CHARS_BAND == 1.5
    assert settings.ROUTING_MAX_TYPE3_RATIO == 0.5
    assert settings.ROUTING_MIN_EMBEDDED_RATIO == 0.5
    assert settings.ROUTING_L2_RENDER_DPI == 100
    assert settings.ROUTING_VERIFY_OCR_TIMEOUT_SEC == 90
    assert settings.ROUTING_VERIFY_OCR_EVIDENCE_ENABLED is True
    assert settings.ROUTING_FULLPAGE_IMAGE_COVERAGE == 0.9


def test_issue273_routing_mode_validator_rejects_unknown():
    import pytest
    from pydantic import ValidationError
    from app.core.config import Settings
    with pytest.raises(ValidationError):
        Settings(RECOGNITION_ROUTING_MODE="yolo")
