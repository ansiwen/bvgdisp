#!/bin/sh
# Compares the frames of the working tree's display code with those of a
# git revision, frame buffer by frame buffer (see frames.py):
#
#   tools/compare_frames.sh [REV]      (default: HEAD)
#
# Identical output is expected from changes that shouldn't be visible, eg.
# optimizations. It also checks that the display thread allocates nothing,
# and that the comparison would catch a difference (--mutate).
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$HERE/..
REV=${1:-HEAD}
MP=$HERE/.deps/micropython/ports/unix/build-standard/micropython
[ -x "$MP" ] || { echo "$MP is missing, run tools/setup.sh first"; exit 1; }
OUT=$HERE/out/frames
rm -rf "$OUT"
mkdir -p "$OUT/rev"
git -C "$ROOT" archive "$REV" src | tar -x -C "$OUT/rev"

run() {
    "$MP" -X heapsize=32M "$HERE/frames.py" "$@" | grep -v "^updated \|^display\|^render task\|^waiting\|^missing"
}
status=0
for dim in 10 3; do
    run "$OUT/rev/src" $dim "$OUT/rev-$dim.bin"
    run "$ROOT/src" $dim "$OUT/new-$dim.bin"
    if cmp -s "$OUT/rev-$dim.bin" "$OUT/new-$dim.bin"; then
        echo "dimming $dim: identical to $REV"
    else
        echo "dimming $dim: DIFFERENT from $REV, first at frame $(( $(cmp "$OUT/rev-$dim.bin" "$OUT/new-$dim.bin" | sed 's/.*char \([0-9]*\).*/\1/') / 32 ))"
        status=1
    fi
done
run "$ROOT/src" 10 "$OUT/mutant.bin" --mutate
if cmp -s "$OUT/new-10.bin" "$OUT/mutant.bin"; then
    echo "self-test FAILED: a changed pen gives the same frames"
    status=1
else
    echo "self-test: a changed pen is detected"
fi
run "$ROOT/src" 10 - --mem
exit $status
