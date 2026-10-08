# syntax=docker/dockerfile:1
# Rip Station for the UGREEN NAS (linux/amd64): makemkvcon + engine + dashboard.
ARG MAKEMKV_VERSION=2.0.0

# --- makemkvcon: open-source libs compiled here, closed-source binary from makemkv-bin ---
FROM python:3.12-slim-bookworm AS makemkv
ARG MAKEMKV_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config libc6-dev libssl-dev libexpat1-dev libavcodec-dev zlib1g-dev \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL https://www.makemkv.com/download/makemkv-oss-${MAKEMKV_VERSION}.tar.gz | tar xz \
    && curl -fsSL https://www.makemkv.com/download/makemkv-bin-${MAKEMKV_VERSION}.tar.gz | tar xz
RUN cd makemkv-oss-${MAKEMKV_VERSION} \
    && ./configure --disable-gui --prefix=/usr \
    && make -j"$(nproc)" \
    && make install DESTDIR=/out
# makemkv-bin is closed source and ships under its own EULA (makemkv-bin/src/eula_en_linux.txt).
# Whoever builds this image must read and accept it: build with MAKEMKV_ACCEPT_EULA=yes.
# The marker file is how makemkv-bin's Makefile records that acceptance.
ARG MAKEMKV_ACCEPT_EULA=no
RUN [ "$MAKEMKV_ACCEPT_EULA" = "yes" ] || { echo "Read the MakeMKV EULA, then build with MAKEMKV_ACCEPT_EULA=yes" >&2; exit 1; }; \
    cd makemkv-bin-${MAKEMKV_VERSION} \
    && mkdir -p tmp && echo accepted > tmp/eula_accepted \
    && make install DESTDIR=/out PREFIX=/usr

# --- runtime ---
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 libexpat1 zlib1g libavcodec59 eject ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=makemkv /out/usr/ /usr/
RUN ldconfig && ! ldd /usr/bin/makemkvcon | grep -q "not found"

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ARG RIPSTATION_BUILD=unknown
ENV RIPSTATION_BUILD=${RIPSTATION_BUILD} \
    HOME=/config \
    RIPSTATION_CONFIG=/config/config.toml \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
COPY docker/config.nas.toml /app/config.default.toml
COPY docker/entrypoint.sh /entrypoint.sh
VOLUME /config
EXPOSE 8765
WORKDIR /config
ENTRYPOINT ["/entrypoint.sh"]
CMD ["rip-station", "serve"]
