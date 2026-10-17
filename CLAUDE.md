# CLAUDE.md

BVG (Berlin transit) departure display: a Raspberry Pi Pico W / Pico 2 W (usually a Pimoroni Interstate 75 W) driving a HUB75 LED matrix. It fetches departures from a `transport.rest`-style API and draws them BVG-style. README.md covers hardware, firmware, and end-user setup. This file covers how the code fits together and the rules that aren't visible from the code you're editing; details of a single function belong in comments there.

## Runtime environment

- **Pimoroni MicroPython**, not CPython or stock MicroPython. It depends on the firmware-provided `hub75`, `picographics`, `pngdec`, plus `machine`, `network`, `_thread`, and `micropython` (`const`, `@micropython.native`, `@micropython.viper`). None of this runs on a desktop as is, and there are no tests in the repo. `docs/performance.md` describes how changes were checked without the device: the unix port with stubbed hardware, generated-code inspection, and an ARM emulator.
- `src/` is the device's filesystem root. The MicroPico VS Code extension syncs it (`micropico.syncFolder: "src"`), or you can copy it with `mpremote cp -r src :`. On boot, the device runs `main.py`.
- `typings/` holds MicroPython/Pimoroni stubs, used only by Pylance. Don't edit them.
- Settings persist in `/settings.json` on the device, not in the repo.
- `firmware/build.sh` builds a firmware for the Interstate 75 W (RP2350) with the app and `src/assets/` frozen in. CI runs it on every push. What's frozen is listed in `firmware/manifest.py`, so **a new module must be added there**; files in `src/assets/` are picked up automatically. With this firmware:
  - The frozen `firmware/main.py` runs instead of a `main.py` on the device.
  - Imports look in the device's root first, then in the frozen modules, then in `/lib` (`sys.path` is `['', '.frozen', '/lib']`), and a directory beats a `.py` file of the same name. So a stale file in the device's root silently replaces the frozen one, while a frozen module replaces one in `/lib`.
  - Assets are not files in the firmware: **open them through `assetfiles`**, which prefers the device's file too.

## Files

- `src/main.py`: only `import bvgdisp`.
- `src/bvgdisp.py`: everything except the web UI. It runs at import time: display setup → `banner()` → `asyncio.run(main())`. After a crash it logs the backtrace and resets the device, unless `_RESET_ON_CRASH` is `False` (for debugging).
- `src/settings.py`: settings dict merged over `DEFAULT_SETTINGS`. `set()` writes to flash and raises a flag that `check()` reads and clears. Only keys already in `DEFAULT_SETTINGS` are accepted, so **a new setting must be added there first**.
- `src/crashlog.py`: timestamped backtraces, appended to `/crash.log` on flash.
- `src/web_server.py`: a hand-rolled asyncio HTTP server. The whole config SPA is `src/assets/index.html`, sent from flash in 1 KB chunks so it never sits in RAM. JSON API: `/api/auth/{challenge,login,logout,password}`, `/api/settings` (GET/POST), `/api/restart`. Auth is challenge-response: the client sends `SHA256(PASSWORD_HASH + nonce)`, gets a session token, and sends it back in the `X-Session` header. The default password is `BVGdisp`.
- `font/font_small.py`: the source of the custom 8px bitmap font, as readable bitmaps. A missing glyph is skipped, so add glyphs here for new umlauts or symbols, then run `python3 font/mkfont.py`. It generates `src/font_bvg.py`, the font as `bytes` tables for `render_text()`, which stay in flash when frozen. CI fails if the generated file is out of date.
- `src/hw_conf.py`: picks the panel size (`DISPLAY_INTERSTATE75_*`) and `COLOR_ORDER`. `PANEL` selects the `Hub75SPWM` driver for panels with S-PWM chips, with the chip registers from the optional `SPWM_PROFILE`. This is per-device configuration.
- `src/assetfiles.py`: opens the files in `src/assets/`, from the device or the firmware.
- `src/assets/*.png`: images for the boot banner.

## Architecture of bvgdisp.py

Two cores, with a strict split:

1. **Core 0, asyncio**: three tasks in `main()`:
   - `data_fetch_task`: every few seconds, fetches `GET {API_URL}/stops/{STATION_ID}/departures`, sets the RTC from the HTTP `Date` header, and publishes the departures as `_dep_data`. It picks the most important currently valid warning for the ticker. The first response only sets the clock, so data arrives with the second.
   - `render_task`: once per second, renders the changed ETA/LINE/DEST text into per-row **off-screen text buffers**. It starts the display thread after the first data arrives.
   - `check_night_time_task`: sets `_dimming` (0–10) from `NIGHT_START`/`NIGHT_END`/`TIMEZONE`. The RTC runs in UTC, and `TIMEZONE` is a plain hour offset with no DST handling.
2. **Core 1, `display_thread`**: runs at `_FRAME_RATE` (30 fps), composes each frame by `blit()`ing regions of the text buffers into the framebuffer, and calls `_h75.update()`. It also does destination scrolling, blinking for "now" departures, and the warning ticker that takes over the bottom row from time to time. With `Hub75` it busy-waits for the frame time, which is deliberate, to avoid flicker. With `Hub75SPWM`, `update()` waits for the panel's refresh instead, which runs at `_SPWM_SYNC * _FRAME_RATE`.

Data structures and sync:
- Each text row is one off-screen buffer laid out as `[ETA][LINE][DEST + " - " ...]`, with one lock per column and a `states` list indexed by the `_S_*` constants. The warning ticker has its own wide buffer and lock. All of them are allocated at startup.
- The text buffers have **1 bit per pixel and no color** (`make_textbuf()`). `blit()` draws only the lit pixels, in a pen, so the display thread clears the frame before drawing it. `render_text()` sets the bits and `clear_cols()` clears column ranges.
- The render task takes a column's lock while it redraws that column. **The display thread must never block on a lock**: it tries with `acquire(False)` and skips that part for the frame if it's busy. Keep the renderer's critical sections short.
- **Don't create globals once the display thread runs**, eg. with a new `global` name in a task. There is no GIL on rp2: adding an entry to the module dict while core 1 looks up a global can make that lookup fail with a random `NameError`. Define shared state at module level, before the thread starts.
- Fetching and JSON parsing are memory-heavy: they wait for a finished render cycle (`_safe_to_fetch`) and run after `gc.collect()`.
- Colors are `(R, G, B)` tuples. `make_pen()` turns one into a pen, an RGB888 int, with the dimming applied. ETA, DEST and the ticker are drawn in `_pen_bvg`, which `check_night_time_task` updates with the dimming; each row keeps its LINE pen in `states`. Draw on the display itself through `set_pen(disp, color)`.
- `render_text(s, buf=None, ...)` with `buf=None` only measures text width.

Layout: on 64 px panels the rows start at y=1 with a 9 px pitch. On 32 px panels they start at y=0 with an 8 px pitch. The LINE column grows to fit the widest line name seen, and shrinks back when settings change.

Boot and network: without an SSID, or when connecting fails, the device starts an open AP named `BVGdisplay` and runs only the web server. When it is connected, `ensure_internet_or_reset()` resets the device if 1.1.1.1:80 can't be reached.

## Conventions and gotchas

- Priorities, in this order: 1. performance, but only at hot spots (`display_thread`, `blit()`, and what the render task does every second); 2. GC pressure: no garbage in steady state, and big buffers allocated once at startup; 3. total RAM usage. The firmware with everything frozen in is what counts, so eg. native code costs flash, not RAM.
- `docs/performance.md` has the MicroPython facts this code relies on (what allocates, what stays in flash, how viper and asm_thumb compile), the measurements behind the buffer sizes and `blit()`, and the alternatives that were rejected. Read it before optimizing.
- Code is tuned for speed on the MCU. Module constants use `const()` with a leading underscore, which makes them compile-time and keeps them out of the module dict. Hot functions copy globals into locals and use `@micropython.native`/`viper`. Keep these patterns when you edit hot paths, and avoid allocating inside `display_thread`.
- `blit()` has no bounds checks, so callers are responsible for offsets and lengths; see its docstring. Its inner loop is `blit_asm()`, `@micropython.asm_thumb` with only Thumb-1 instructions, so the same code runs on the RP2040 and the RP2350. The unix port can't run it: check changes to it in an emulator or on both MCUs.
- `time.ticks_ms()` wraps at 2³⁰ ms (about 12.4 days) on rp2. Never compare or add ticks with `>`/`+`. Always use `time.ticks_diff()`/`time.ticks_add()`.
- HTTP requests go through `http_get()`, a minimal HTTP/1.0 client. It has **no timeout**, so wrap every call in `asyncio.wait_for()`. It reads the whole response into `_recv_buf`, allocated once at startup, so a response larger than `_RECV_BUF_SZ` fails the fetch. Nothing parsed from it may keep a reference into the buffer, because the next fetch overwrites it.
- An exception that escapes any task in `main()` ends the whole program. Each task loop must catch its own errors, and data from the API should be handled one entry at a time.
- Settings values aren't validated: the web UI can store `null` (JS `NaN`) or the wrong type. If `settings.json` can't be parsed, everything silently falls back to defaults, which loses WiFi and resets the password, so keep its writes atomic.
- Only core 0 writes to flash. Flash writes stall the other core.
- Send responses through `send_response()`/`send_file()`: one big `writer.write()` makes asyncio copy the unsent rest in one piece, which fails on a fragmented heap.
- `src/main.py.bak_*` files are local backups and untracked. Don't edit or commit them.
- Debug output goes to `print`, over the USB serial REPL. `console()` also writes to the LED panel during boot.
