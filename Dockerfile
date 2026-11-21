FROM golang:1.27.4-trixie AS publisher
WORKDIR /build
COPY publisher/go.mod publisher/go.sum ./
RUN go mod download
COPY publisher/main.go ./
RUN CGO_ENABLED=1 go build -trimpath -buildmode=c-shared -o /build/libcinema_publisher.so .
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl xz-utils nasm pkg-config libva-dev libdrm-dev libx264-dev libffmpeg-nvenc-dev
COPY native/encoder.c ./
# Link only the codecs used by the publisher. PyAV supplies the source demuxers and decoders.
RUN curl -fsSLo ffmpeg.tar.xz https://ffmpeg.org/releases/ffmpeg-7.1.5.tar.xz \
    && echo "de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f  ffmpeg.tar.xz" | sha256sum -c - \
    && tar xf ffmpeg.tar.xz && cd ffmpeg-7.1.5 \
    && ./configure --disable-all --disable-autodetect --disable-programs --disable-doc --enable-pic \
         --enable-avcodec --enable-avutil --enable-swscale --enable-gpl --enable-libx264 \
         --enable-vaapi --enable-libdrm --enable-ffnvcodec --enable-nvenc \
         --enable-encoder=libx264,h264_vaapi,h264_nvenc \
    && grep -qx '#define CONFIG_H264_NVENC_ENCODER 1' config_components.h \
    && grep -qx '#define CONFIG_H264_VAAPI_ENCODER 1' config_components.h \
    && make -j4 libavcodec/libavcodec.a libswscale/libswscale.a libavutil/libavutil.a \
    && cc -O2 -shared -fPIC -I. ../encoder.c -Wl,--exclude-libs,ALL -Wl,-z,defs \
         -o /build/libcinema_encoder.so libavcodec/libavcodec.a libswscale/libswscale.a libavutil/libavutil.a \
         -lx264 -lva-drm -lva -ldrm -lm -ldl -pthread

FROM node:26.13.1-bookworm-slim AS web
WORKDIR /build
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ca-certificates libx264-164 libva2 libva-drm2 libdrm2 intel-media-va-driver-non-free libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 cinema && useradd -u 10001 -g cinema --no-create-home cinema
WORKDIR /app
COPY requirements.txt ./
RUN pip install --requirement requirements.txt
COPY cinema ./cinema
COPY --from=publisher /build/libcinema_*.so ./cinema/
COPY --from=web /build/dist ./web/dist
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]
CMD ["uvicorn", "cinema.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers"]
