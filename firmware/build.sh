#!/bin/sh
# Builds MicroPython firmware for the Interstate 75 W (RP2350) with bvgdisp
# frozen in: firmware/dist/bvgdisp-i75w_rp2350.uf2
#
# Needs git, cmake, python3 and arm-none-eabi-gcc (Ubuntu: cmake
# gcc-arm-none-eabi libnewlib-arm-none-eabi). The dependencies are fetched
# into firmware/.deps on the first run.
set -e

# Pinned dependencies: update them deliberately, not by accident
MICROPYTHON=pimoroni/micropython@725235d49042b9c6687588d9fea0b49bdea2755d     # cyw43-dynamic-pins, v1.29.0
INTERSTATE75=pimoroni/interstate75@c84cdf4dc637bb98afc6806b2bde115ee09562dd   # i75w_rp2350 board
PIMORONI_PICO=ansiwen/pimoroni-pico@690f37067397e2d39635da70ed766ef6e07a2e1a  # hub75-spwm: Hub75SPWM

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$HERE/../src
DEPS=$HERE/.deps
BUILD=$HERE/build
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}

# fetch <owner/repo@commit> <dir>: a shallow checkout of exactly that commit
fetch() {
    if [ "$(git -C "$2" rev-parse HEAD 2>/dev/null)" = "${1#*@}" ]; then
        return
    fi
    rm -rf "$2"
    git init -q "$2"
    git -C "$2" fetch -q --depth 1 "https://github.com/${1%@*}" "${1#*@}"
    git -C "$2" checkout -q FETCH_HEAD
}

fetch "$MICROPYTHON" "$DEPS/micropython"
for module in pico-sdk cyw43-driver lwip mbedtls micropython-lib tinyusb btstack; do
    git -C "$DEPS/micropython" submodule update -q --init --depth 1 "lib/$module"
done
fetch "$INTERSTATE75" "$DEPS/interstate75"
fetch "$PIMORONI_PICO" "$DEPS/pimoroni-pico"
git -C "$DEPS/pimoroni-pico" submodule update -q --init --depth 1

mkdir -p "$BUILD" "$HERE/dist"
python3 "$HERE/mkassets.py" "$BUILD/frozen_assets.py" \
    "$SRC/index.html" "$SRC/ansiwen128x64.png" "$SRC/bvgdisp_logo.png"
make -C "$DEPS/micropython/mpy-cross" -j "$JOBS" >/dev/null

BOARD=$DEPS/interstate75/boards/i75w_rp2350
# Shown in the REPL banner
export MICROPY_GIT_TAG="bvgdisp $(git -C "$HERE" describe --always --dirty 2>/dev/null || echo unknown)"
export MICROPY_GIT_HASH="$MICROPY_GIT_TAG"
cmake -S "$DEPS/micropython/ports/rp2" -B "$BUILD/rp2" \
    -DMICROPY_BOARD=i75w_rp2350 \
    -DMICROPY_BOARD_DIR="$BOARD" \
    -DUSER_C_MODULES="$BOARD/usermodules.cmake" \
    -DPIMORONI_PICO_PATH="$DEPS/pimoroni-pico" \
    -DMICROPY_FROZEN_MANIFEST="$HERE/manifest.py" \
    -DPICO_BUILD_DOCS=0 \
    -DPICO_NO_COPRO_DIS=1
cmake --build "$BUILD/rp2" -j "$JOBS"

cp "$BUILD/rp2/firmware.uf2" "$HERE/dist/bvgdisp-i75w_rp2350.uf2"
echo "Built $HERE/dist/bvgdisp-i75w_rp2350.uf2"
