#!/bin/sh
# Build the NAS image on any machine with Docker (Mac or PC) and export it for UGOS Docker -> Image -> Import.
set -e
cd "$(dirname "$0")/.."
if [ "$MAKEMKV_ACCEPT_EULA" != "yes" ]; then
    echo "This image includes MakeMKV, which has its own license:"
    echo "  src/eula_en_linux.txt inside https://www.makemkv.com/download/makemkv-bin-2.0.0.tar.gz"
    echo "Read it, then run:  MAKEMKV_ACCEPT_EULA=yes $0"
    exit 1
fi
BUILD="$(date '+%Y-%m-%d %H:%M')"
docker buildx build --platform linux/amd64 --build-arg "RIPSTATION_BUILD=$BUILD" \
    --build-arg MAKEMKV_ACCEPT_EULA=yes \
    -t rip-station:latest -t "rip-station:$(date '+%Y%m%d-%H%M')" --load .
echo "build stamp: $BUILD"
mkdir -p dist
docker save rip-station:latest | gzip > dist/rip-station-amd64.tar.gz
ls -lh dist/rip-station-amd64.tar.gz
