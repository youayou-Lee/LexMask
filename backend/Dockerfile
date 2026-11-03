FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime dependencies for the lightweight API image.
# LibreOffice is baked in (full dpkg install). History: Issue#217 recorded a
# ~12.4GB RSS peak during dpkg unpack and switched to mounting the HOST's
# soffice read-only instead; that mount broke on hosts whose libc/ICU don't
# match the image's Debian base (2026-11-03: on Ubuntu 25.x hosts soffice
# forks die on ~30 missing host-only libs and .doc conversion returns
# placeholder error text). A 2026-11-03 measurement on python:3.11-slim
# (trixie, LO 25.2) put the whole install's peak dpkg/apt RSS at ~73MB and
# `soffice --version` passes in-image — so we bake it properly. If a future
# base changes this picture, re-measure before reintroducing hacks.
# APT_MIRROR 置空 ⇒ 保留 deb.debian.org 官方源（GitHub Actions 海外 runner 走阿里云镜像实测
# 卡死 18min+，2026-11-03 三轮复现；官方源 2.5min 通过）。本地构建不传参即默认阿里云。
ARG APT_MIRROR=mirrors.aliyun.com
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libxinerama1 libpng16-16t64 libreoffice-writer-nogui; \
    rm -rf /var/lib/apt/lists/*

# Keep model-serving stacks out of the default backend image. GPU/OCR/vision
# dependencies are installed by their profile-gated service images instead.
# Note: requirements-ci.txt (floating versions) is used intentionally.
# requirements.lock is NOT suitable here: it pins the full local dev stack
# (paddlepaddle-gpu, paddleocr) that this slim image must exclude, while
# missing runtime deps this image needs (SQLAlchemy, PyMySQL, psycopg,
# numpy, opencv-python-headless, openpyxl).
COPY requirements-ci.txt .
# Issue#217: pip index is overridable at build time. Default = Aliyun mirror
# (direct domestic connection — the image build environment may not inherit
# the host's SOCKS proxy, where a direct pypi.org route stalls/hangs; TUNA's
# pypi mirror was also observed returning 403 on package GETs on 2026-10-31).
# Pass --build-arg PIP_INDEX_URL=https://pypi.org/simple to restore the stock index.
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --index-url "$PIP_INDEX_URL" -r requirements-ci.txt

COPY . .

RUN mkdir -p /app/data /app/uploads /app/outputs

RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
