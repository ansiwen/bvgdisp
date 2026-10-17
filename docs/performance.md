# Performance and memory notes

What was verified, measured and decided while removing dynamic allocations
and the RGB888 text buffers (October 2026). The priorities that decide
trade-offs are in `CLAUDE.md`: performance at hot spots, then GC pressure,
then total RAM, judged on the firmware with everything frozen in.

Everything below refers to the MicroPython that `firmware/build.sh` pins
(Pimoroni fork, v1.29.0). Source references are to that tree. Re-check them
when the pin changes.

## MicroPython facts this code relies on

Memory:

- A **bytecode function's frame** goes on the heap if it is larger than 11
  words (`VM_MAX_STATE_ON_STACK`, `py/objfun.c`), and is freed right after
  the call. That is not garbage, but every call is a heap allocation, and on
  a full heap that allocation runs a whole `gc.collect()` first. So functions
  with many locals that run every second (`render_row`, `clear_row`) are
  `@micropython.native`, whose frames are always on the C stack. `mpy-tool.py
  -d` prints a function's frame size as the first number of its `prelude`.
- **Frozen native, viper and asm code stays in flash** (`static const`
  arrays in `.text`, MicroPython's `tools/mpy-tool.py`), so it costs no RAM. From a `.py`
  file on the device it is compiled into RAM, once at import.
- **Frozen `bytes` and `str` constants stay in flash.** That's why the font
  is generated as `bytes` tables (`font/mkfont.py`). Dict and list literals
  are always built in RAM at import, even when frozen: the old font dict
  took about 18 KB.
- These **don't allocate**:
  - iterating a `str`, which yields interned single characters
    (`py/objstrunicode.c`);
  - `str.replace()` when nothing matches, which returns the same object;
  - tuple unpacking;
  - calls with keyword arguments to `@micropython.native` functions (tested
    only for those).
- These **do allocate**:
  - `f(*args)`, even into a C method: `create_pen(*color)` did;
  - `tuple(<generator>)`;
  - `"a" + b`, f-strings, `str(n)`.

  `print(a, b)` with separate arguments doesn't build a string.
- `json.loads()` accepts any buffer object (`extmod/modjson.c`), so it
  parses a `memoryview` slice of `_recv_buf` without copying it. The parsed
  objects are copies, not slices of the buffer.
- asyncio's `Stream.read()` and `readexactly()` do `r += r2` for every TCP
  segment, so a response is copied over and over and needs up to twice its
  size in one piece. `readinto()` doesn't, which is why `http_get()` uses
  it.
- **`micropython.heap_lock()` is per thread** (`gc_lock_depth` is in the
  thread state, `py/gc.c`). It can be used in `display_thread` alone to
  catch allocations there. It does **not** catch bytecode frames: under a
  heap lock they silently fall back to the C stack. Measure those with
  `gc.mem_alloc()` deltas, with `gc.disable()`.
- An unbalanced `micropython.heap_unlock()` underflows the counter and
  leaves the heap locked.

Viper code generation (read off `mpy-cross -march=armv7emsp` and `armv6m`
output):

- Only the **first two locals** live in registers (`r4`, `r5`). For a
  function, those are its first two parameters, so a hot loop should reuse
  its parameters as running variables. Everything else is on the stack.
- Each operation goes through `r0`-`r2` with extra `mov`s: an `if b & K:`
  bit test is 6 instructions (7 for masks above 16 bits, which need
  `movw`/`movt`), and a loop condition `a < b` is about 10, because it first
  makes a 0/1 value.
- Indexing with a **constant** (`dest[3] = pen`) compiles to a single
  `ldr`/`str` with an immediate offset. Advancing a pointer
  (`p = ptr32(uint(p) + 4)`) costs 4 instructions. So unroll with constant
  offsets and advance once per block.
- Integer literals from `0x40000000` up don't fit a small int. They need
  `uint(...)` and are loaded from the constant table by a runtime call, so
  hoist them out of loops. Indexing a `ptr32` gives an `int`, so cast with
  `uint()` before mixing it with `uint`s.

Inline assembler (`@micropython.asm_thumb`, `py/emitinlinethumb.c`):

- The RP2040's Cortex-M0+ has only Thumb-1. `it` and the other ARMv7-M
  instructions are rejected for `-march=armv6m`. Code that sticks to
  Thumb-1 gets the same machine code for both MCUs. `blit_asm()` uses
  conditional branches instead of `it`.
- Names: `lsl`/`lsr` (not `lsls`); `and_`; `it(cs)` takes a lowercase
  condition. `ldr`/`str` immediate offsets are 0-124 bytes (16-bit
  encoding), which is 32 words from one base register.
- At most 4 arguments (`r0`-`r3`), so `blit()` passes a parameter block.
  `bytearray` arguments arrive as pointers to their data, ints as values.
  `r4`-`r7` may be used: the prologue saves them.

## Measurements

**Executed instructions per pixel of `blit()`:** counted by running the
`mpy-cross` output in an emulator on rows built from real station names:
ETA, LINE, DEST and a scrolling DEST.

| `blit()` | RP2350 | RP2040 |
|---|---|---|
| RGB888 copy (until 2026-10) | 13.35 | 13.42 |
| 1-bit, viper | 11.30 | 11.95 |
| 1-bit, `blit_asm()` | 3.55 | 3.55 |

The viper wrapper in front of `blit_asm()` adds an estimated 300-600
instructions per call (global lookups, an exception frame, the call). That
part runs in the C runtime and wasn't emulated.

**Frame timing on the device** (RP2350, 128x64 S-PWM panel, 7 rows,
2026-10), measured with `_FRAME_STATS = const(True)`. That prints averages
and maxima over 300 frames on the USB serial console every 10 s:

| | avg | max |
|---|---|---|
| clearing the frame buffer | 0.49 ms | 0.7 ms (once 1.0) |
| drawing (frame logic and all `blit()`s) | 0.9-1.0 ms | 1.5 ms |
| `_h75.update()`, waiting for the panel refresh | 31.9 ms | 33.5-35.3 ms |
| frame period | 33.33 ms | 34.8-36.7 ms |

So the display thread works about 1.4 ms of the 33.3 ms frame, and making
`blit()` faster would gain nothing visible. In every 10 s window, at least
one frame came 1.5-3.4 ms late, because `update()` returned late, not
because of drawing. Probably that's core 0 (fetch, JSON parsing, GC)
competing for flash and RAM. With S-PWM, such a frame is shown for one
extra refresh. There is no measurement of the code before these changes
to compare with.

**API responses** (14 results, busy stations, 2026-10):

| API | Response | Parsed tree (unix port, 64-bit) |
|---|---|---|
| `http://vbb.a6n.de` | 1.8-3.3 KB | 12-16 KB |
| `https://v6.bvg.transport.rest` | 17-21 KB | 96-110 KB |

v6.bvg.transport.rest sends no `"warnings"` key and ignores `sparse`. Over
HTTPS, every connection also allocates tens of KB in mbedtls, which can't be
made static: one more reason for the plain-HTTP proxy.

**Text widths** with the real font:

- the longest destination seen at 10 busy stations was 180 px, including
  `" - "`;
- the longest warning in ticker form was 1372 px (321 characters).

The ticker shows for 45 s at 1 px per frame, about 1350 px per showing.

**Free heap** on the RP2350 after the 1-bit buffers: about 286 KB (`memfree`
after a fetch, so it still includes that fetch's garbage). The bigger buffers
since then cost about 18 KB of it.

## How to check changes without the device

`tools/` has these checks ready to run, see `tools/README.md`. How they work:

1. **Build the pinned MicroPython**: the commit from `firmware/build.sh`,
   then `make -C mpy-cross` and `make -C ports/unix`. The unix port runs
   native and viper code, but not `asm_thumb`.
2. **Inspect generated code**:
   - `mpy-cross -march=armv7emsp` (RP2350) or `-march=armv6m` (RP2040),
     then MicroPython's `tools/mpy-tool.py -f -q <qstrdefs> file.mpy` writes C with each
     function's machine code as a `fun_data_*` byte array;
   - native functions keep their names, viper and asm ones are named after
     the file, in source order (`tools/mpycode.py` maps them back);
   - wrap the bytes in `.byte` directives, assemble with
     `llvm-mc -triple=thumbv8m.main-none-eabi -mcpu=cortex-m33`, and
     disassemble with `llvm-objdump -d`.
   For the RP2040, use `thumbv6m`/`cortex-m0`: anything that isn't
   Thumb-1 then doesn't decode.
3. **Run machine code** in `unicorn` (`pip install unicorn`), as
   Cortex-M0 or M33, to test it against a Python reference and count
   executed instructions:
   - viper code calls into the runtime only to convert its arguments, so a
     fake function object, whose function table points to a `bx lr`
     identity stub, is enough;
   - unicorn's Cortex-M0 model does **not** reject Thumb-2 instructions,
     so it can't prove RP2040 compatibility; the disassembly can (2).
4. **Compare whole frames**. Run `bvgdisp.py` in the unix port with:
   - stub modules for `machine`, `network`, `hub75`, `picographics` (an
     RGB888 framebuffer) and `hw_conf`;
   - a deterministic fake `time`;
   - `asyncio` sleeps that only yield.

   Then drive `render_task` and `display_thread` with fixed departures and
   a warning, and hash the framebuffer in `Hub75.update()`. Old and new code
   must give identical hashes, frame by frame. Plant a one-pixel bug once to
   see that the test catches it. On the unix port, `time.time()` returns a
   float, unlike on rp2. Replace `blit_asm()` there with a viper stand-in
   that reads the same parameter block.

## Decisions and rejected alternatives

- **1-bit text buffers, rows, LSB = leftmost pixel**: chosen over columns.
  Rows store 8 consecutive pixels per byte, so the stores use constant
  offsets, and a 32-bit load gives 32 pixels in display order. Columns
  need a stride add per pixel (4 instead of 3 instructions in assembler,
  11 instead of 6 in viper), but would make rendering simpler.
- **Assembler instead of viper for the blit loop**: in viper, the best
  1-bit loop is only about 15 % faster than the old RGB888 copy, because
  every bit test costs 6 instructions. A constant-offset viper copy of
  RGB888 buffers would have reached about 3 instructions per pixel, but
  without the RAM saving. The 1-bit format only pays off for speed in
  assembler.
- **Not done: a streaming JSON scanner** instead of `json.loads()`. It would
  remove the per-fetch tree (above), but a hand-written parser is the most
  bug-prone option. With the proxy's small responses, measure on the
  device first (`gc.mem_alloc()` around a fetch,
  `micropython.mem_info(1)` after hours) before doing it.
- **Not done**: parsing timestamps without slices, a single watchdog
  instead of `asyncio.wait_for()`, header parsing in the web server. They
  are small or rare compared to the parsed tree.
