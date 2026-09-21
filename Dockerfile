FROM python:3.12-slim

ARG STREAMED_M3U_VERSION=dev
ARG IMAGE_SOURCE=""

LABEL org.opencontainers.image.title="streamed-m3u" \
      org.opencontainers.image.description="Live sports M3U playlist and XMLTV guide server with headless-Chromium stream resolution" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${STREAMED_M3U_VERSION}" \
      org.opencontainers.image.source="${IMAGE_SOURCE}"

# PLAYWRIGHT_BROWSERS_PATH has to be set before `playwright install` runs, so
# the browsers land somewhere the non-root runtime user can reach. The
# default is under /root/.cache, and /root is mode 0700.
ENV PYTHONUNBUFFERED=1 \
    STREAMED_M3U_VERSION=${STREAMED_M3U_VERSION} \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libatspi2.0-0 \
    curl wget tini procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium && playwright install-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY app.py extract_stream.py dashboard.py settings.py auth.py dockerctl.py dispatcharr_sync.py lineup.py entrypoint.sh ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY seed/ ./seed/
COPY tools/ ./tools/
RUN chmod 0755 /app/entrypoint.sh && mkdir -p /data

# The roster, the persisted extraction cache and console settings live here.
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT:-8787}/health')" || exit 1

EXPOSE 8787

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python", "app.py"]
