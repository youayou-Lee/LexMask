FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime dependencies for the lightweight API image.
# Issue#217: LibreOffice is NOT baked into this image. dpkg's unpack of the
# LibreOffice package set peaks at ~12.4GB RSS (swap exhausted, earlyoom kills
# apt on small dev hosts; DocumentFoundation mirrors also block bulk GET), so
# the runtime mounts the host's soffice read-only instead (see compose):
#   /usr/lib/libreoffice + /usr/bin/soffice + /etc/libreoffice
# Hosts running this Docker deployment therefore need:
#   sudo apt-get install --no-install-recommends libreoffice-writer-nogui
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libxinerama1 libpng16-16t64 \
    && rm -rf /var/lib/apt/lists/*

# Keep model-serving stacks out of the default backend image. GPU/OCR/vision
# dependencies are installed by their profile-gated service images instead.
# Note: requirements-ci.txt (floating versions) is used intentionally.
# requirements.lock is NOT suitable here: it pins the full local dev stack
# (paddlepaddle-gpu, paddleocr) that this slim image must exclude, while
# missing runtime deps this image needs (SQLAlchemy, PyMySQL, psycopg,
# numpy, opencv-python-headless, openpyxl).
COPY requirements-ci.txt .
# Issue#217: the mounted /usr/bin/soffice wrapper resolves its launcher
# relative to its own directory (/usr/bin/oosplash); symlink it to the real
# launcher inside the mounted LibreOffice tree.
RUN ln -s /usr/lib/libreoffice/program/oosplash /usr/bin/oosplash
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
