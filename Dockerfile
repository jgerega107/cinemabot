FROM node:24.22.1-bookworm-slim AS web
WORKDIR /build
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.15-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libva2 libva-drm2 libdrm2 intel-media-va-driver libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 cinema && useradd -u 10001 -g cinema --no-create-home cinema
WORKDIR /app
COPY requirements.txt ./
RUN pip install --requirement requirements.txt
COPY cinema ./cinema
COPY --from=web /build/dist ./web/dist
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]
CMD ["uvicorn", "cinema.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers"]
