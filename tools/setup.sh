#!/bin/sh
# Builds what the tools in tools/ need, into tools/.deps:
# - mpy-cross and the unix port of the MicroPython that firmware/build.sh
#   pins, so the code is compiled and run like on the device
# - a Python venv with unicorn, the ARM emulator for blit_emu.py
#
# Needs git, make, a C compiler and python3. disasm.py and blit_emu.py
# also use llvm-mc and llvm-objdump (Ubuntu: llvm), if installed.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
DEPS=$HERE/.deps
# the same commit as the firmware
PIN=$(sed -n 's/^MICROPYTHON=\([^ ]*\).*/\1/p' "$HERE/../firmware/build.sh")
REPO=${PIN%@*}
COMMIT=${PIN#*@}
JOBS=${JOBS:-$(nproc 2>/dev/null || echo 4)}

mkdir -p "$DEPS"
if [ "$(git -C "$DEPS/micropython" rev-parse HEAD 2>/dev/null)" != "$COMMIT" ]; then
    rm -rf "$DEPS/micropython"
    git init -q "$DEPS/micropython"
    git -C "$DEPS/micropython" fetch -q --depth 1 "https://github.com/$REPO" "$COMMIT"
    git -C "$DEPS/micropython" checkout -q FETCH_HEAD
fi
make -C "$DEPS/micropython/mpy-cross" -j "$JOBS" >/dev/null
make -C "$DEPS/micropython/ports/unix" submodules >/dev/null
make -C "$DEPS/micropython/ports/unix" -j "$JOBS" >/dev/null

if [ ! -x "$DEPS/venv/bin/python" ]; then
    python3 -m venv "$DEPS/venv"
fi
"$DEPS/venv/bin/pip" install -q unicorn

echo "Built in $DEPS:"
echo "  $DEPS/micropython/mpy-cross/build/mpy-cross"
echo "  $DEPS/micropython/ports/unix/build-standard/micropython"
echo "  $DEPS/venv (unicorn)"
