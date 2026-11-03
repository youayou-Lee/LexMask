"""Issue #92：.doc（旧版 Word）解禁后的上传校验。

9/15 判「转换链不可用」实为实例宿主未装 LibreOffice（#46 实测时的环境缺口），
2026-09-18 dcu-main 实例探针（含 WPS 真实样本）转换/解析/脱敏 0 残留。
本文件守住三道门：白名单放行 .doc、真 OLE 魔数通过、非 OLE 二进制/文本
伪装 .doc 仍被拒。
"""
import os
import tempfile

from app.core.config import settings
from app.core.file_validation import (
    detect_content_type,
    get_file_type,
    spoof_reject_message,
    validate_extension,
    validate_magic_bytes,
)

# WPS/MS Word .doc 的 OLE 复合文档魔数（Composite Document File V2）
OLE_DOC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20
UTF8_TEXT = "被告赵涛洋，身份证号110118197712242590。".encode()
DOCX_ZIP = b"PK\x03\x04" + b"\x00" * 20


def _write(data: bytes, name: str) -> str:
    path = os.path.join(tempfile.mkdtemp(), name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def test_doc_in_allowed_extensions():
    assert ".doc" in settings.ALLOWED_EXTENSIONS


def test_doc_extension_accepted():
    assert validate_extension(".doc") is True


def test_real_ole_doc_magic_passes():
    assert validate_magic_bytes(_write(OLE_DOC, "x.doc"), ".doc") is True


def test_utf8_text_disguised_as_doc_rejected():
    # acceptance-53.doc 真实情形：UTF-8 文本伪造 .doc 扩展名
    assert validate_magic_bytes(_write(UTF8_TEXT, "x.doc"), ".doc") is False


def test_docx_bytes_under_doc_extension_pass():
    # docx 内容改名 .doc：soffice 可直接转换，放行
    assert validate_magic_bytes(_write(DOCX_ZIP, "x.doc"), ".doc") is True


def test_doc_maps_to_doc_file_type():
    assert get_file_type("合同.doc") is not None


# ---------------------------------------------------------------------------
# 2026-11-03 用户试用反馈：报错必须说清「实际检测到的格式」——用户传了
# UTF-8 文本伪装的 .doc，只看到「可能是伪造文件」黑话，误以为产品坏了。
# ---------------------------------------------------------------------------

def test_detect_content_type_utf8_text():
    assert detect_content_type(_write(UTF8_TEXT, "x.bin")) == "纯文本"


def test_detect_content_type_ole_doc():
    assert "Word" in detect_content_type(_write(OLE_DOC, "x.bin"))


def test_detect_content_type_pdf():
    assert detect_content_type(_write(b"%PDF-1.7\nrest", "x.bin")) == "PDF 文档"


def test_detect_content_type_unknown_binary():
    assert detect_content_type(_write(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08", "x.bin")) == "未知二进制格式"


def test_spoof_reject_message_names_actual_type_and_action():
    msg = spoof_reject_message(_write(UTF8_TEXT, "x.doc"), ".doc")
    assert "纯文本" in msg
    assert ".doc" in msg
    # 必须给可操作指引，且不再出现「伪造文件」这类开发者黑话
    assert "另存" in msg or "重新" in msg
    assert "伪造" not in msg
