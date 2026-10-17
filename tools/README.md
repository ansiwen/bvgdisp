# Tools

Checks for changes, without the device. They run the code with the
MicroPython that the firmware pins, on the desktop, and the assembler in an
ARM emulator. They are not unit tests: each answers one question that came
up when optimizing (see `docs/performance.md`), so pick the ones that fit a
change.

## Setup

    tools/setup.sh

This builds `mpy-cross` and the unix port of the pinned MicroPython into
`tools/.deps` (a few minutes, once), and a venv with `unicorn`. It needs
git, make, a C compiler and python3. `disasm.py` and `blit_emu.py` also use
`llvm-mc` and `llvm-objdump` (Ubuntu: `apt install llvm`).

Below, `MP` is `tools/.deps/micropython/ports/unix/build-standard/micropython`.

## The tools

| Tool | Question | Run |
|---|---|---|
| `compare_frames.sh` | Does the display look the same as at a git revision, frame by frame? Does the display thread still allocate nothing? | `tools/compare_frames.sh [REV]` |
| `frames.py` | The harness behind it: frame hashes of one `src/` tree, `--mem` allocations per frame, `--long` texts longer than the buffers | `$MP -X heapsize=32M tools/frames.py src 10 out.bin [--mem\|--long\|--mutate]` |
| `blit_emu.py` | Is `blit_asm()` right on both MCUs, Thumb-1 only, and how fast? | `tools/.deps/venv/bin/python tools/blit_emu.py` |
| `disasm.py` | What machine code does a native, viper or asm function compile to? | `python3 tools/disasm.py [--arch armv6m] [file.py] [function]` |
| `test_web.py` | Does the login work, and are the nonce and session tables bounded? | `$MP tools/test_web.py` |
| `measure_responses.py` | How wide are real destinations and warnings, and how much heap does a parsed response take? | `$MP tools/measure_responses.py resp.json ...` |

Notes:

- `compare_frames.sh` stubs the hardware: `picographics` is an RGB888
  framebuffer, `hub75.update()` hashes it, and the clock and the data are
  fixed. It runs the revision's whole `src/` tree, so older revisions with
  other buffer formats can be compared too. It also changes a pen once, to
  check that a difference would be caught.
- The unix port can't run `asm_thumb`. `frames.py` replaces `blit_asm()` with
  a viper stand-in that reads the same parameter block. `blit_emu.py` checks
  the real machine code against a reference, so the two together cover
  `blit()`. Keep the parameter block in sync between `blit()`, the stand-in
  and `blit_emu.py`.
- `frames.py` checks every `blit()` call against the bounds that its
  docstring requires.
- The unix port is 64-bit: pointers and parsed objects are about twice
  their size on the device. `time.time()` returns a float there, so
  `frames.py` fakes the clock with ints, as on rp2.
- unicorn's Cortex-M0 model accepts Thumb-2 instructions, so only the
  disassembly (step 2 of `blit_emu.py`) shows that code runs on the RP2040.
- Viper and asm functions are only named by file in `mpy-tool.py`'s
  output. `mpycode.py` maps them to their names by source order.
