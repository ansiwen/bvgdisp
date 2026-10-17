import time
import socket
import machine
import network
import asyncio
import json
import hub75
import picographics
import font_bvg
import settings
import hw_conf
import gc
import _thread
import crashlog

_ETA_WIDTH = const(11)
_COL_GAP = const(3)

# widths in pixels of the 1-bit text buffers, multiples of 32, see blit().
# The longest destination seen was 180 px, the longest warning 1372 px; the
# ticker shows for _WARN_DURATION_MS at 1 px per frame, about 1350 px.
_TEXT_BUF_SZ = const(512)
_WARN_BUF_SZ = const(4096)
# a whole HTTP response, headers included. With the default 14 results,
# v6.bvg.transport.rest sends up to about 21 KB, vbb.a6n.de up to 3.5 KB
# (busy stations, 2026); the rest is room for more remarks or results
_RECV_BUF_SZ = const(48 * 1024)

_FRAME_RATE = const(30)
# Frame timing: True makes the display thread measure every frame, and the
# render task print averages and maxima every _FRAME_STATS_N frames. With
# False the compiler drops all of it.
_FRAME_STATS = const(False)
_FRAME_STATS_N = const(300)
# S-PWM panels: refresh at _SPWM_SYNC * _FRAME_RATE, each frame is shown for
# exactly _SPWM_SYNC refreshes
_SPWM_SYNC = const(2)
_DEST_SCROLL_DELAY = const(5)
_BLINK_DELAY_MS = const(600)
_FETCH_DELAY = const(10)
_FETCH_TIMEOUT = const(20)
_RENDER_CYCLE_MS = const(1000)
_WARN_DURATION_MS = const(45 * 1000)
_WARN_PAUSE_MS = const(30 * 1000)

# Colors as tuples (R, G, B)
_RED = const((120, 0, 0))
_YELLOW = const((255, 180, 0))
_BVG = const((255, 170, 0))
_WHITE = const((255, 255, 255))

_S_ETA = const(0)
_S_LINE = const(1)
_S_DEST = const(2)
_S_DEST_SZ = const(3)
_S_DEST_X = const(4)
_S_BLINKING = const(5)
_S_LINE_PEN = const(6)
_S_LINE_SZ = const(7)  # the width of _S_LINE

_FRAME_DELAY_MS = const(1000//_FRAME_RATE)
_DEST_SCROLL_INIT = const(-_FRAME_RATE*_DEST_SCROLL_DELAY)
_WARN_CYCLE_MS = const(_WARN_DURATION_MS+_WARN_PAUSE_MS)
_LINE_MIN_WIDTH = const(13)
# seconds added before flooring the ETA to minutes, so it rounds up from
# 15 s past a full minute: 0-14 s shows "now", 15-74 s 1', 75 s 2'
_ETA_ROUND_S = const(45)

# Reset the device when the program crashes. Set to False for debugging,
# to keep the device stopped at the REPL after the backtrace.
_RESET_ON_CRASH = const(True)

_rtc = machine.RTC()

# Enable the Wireless
network.country("DE")
network.hostname("BVGdisplay")

_wlan = network.WLAN(network.STA_IF)

# Setup for the display

def make_textbuf(sz):
    """A text buffer of sz pixels by 8 lines, 1 bit per pixel: line after
    line, sz // 8 bytes each, the leftmost pixel in bit 0 of the first
    byte. Without color: blit() draws it in a pen. The 4 bytes at the end
    are padding, see blit()."""
    return bytearray(sz + 4)

_disp = picographics.PicoGraphics(display=hw_conf.DISPLAY)
_disp_mv = memoryview(_disp)
_disp.set_pen(0)
_disp.clear()
_disp_width, _disp_height = _disp.get_bounds()

_row_y0 = 1 if _disp_height == 64 else 0
_row_height = 9 if _disp_height == 64 else 8
_n_textlines = (_disp_height - _row_y0) // _row_height

# hw_conf.PANEL selects a panel with S-PWM driver chips, eg. hub75.PANEL_SM16380SH,
# and the optional hw_conf.SPWM_PROFILE its registers, eg. hub75.SM16380SH_REG8
_spwm = hasattr(hw_conf, "PANEL")
if _spwm:
    _h75 = hub75.Hub75SPWM(_disp_width, _disp_height, panel_type=hw_conf.PANEL,
                           color_order=hw_conf.COLOR_ORDER)
    _h75.set_refresh_rate(_SPWM_SYNC * _FRAME_RATE)
    if hasattr(hw_conf, "SPWM_PROFILE"):
        _h75.set_config_registers(hw_conf.SPWM_PROFILE)
else:
    _h75 = hub75.Hub75(_disp_width, _disp_height, color_order=hw_conf.COLOR_ORDER)
_h75.start()

# State shared between the tasks and the display thread. The warning
# ticker's state is set up after banner(), see the end of this file.
# Every global must exist before the display thread starts: there is no GIL
# on rp2, and adding a global while core 1 looks one up can make that lookup
# fail with a NameError.
_dimming = 10  # 0-10, set by check_night_time_task
_dest_offset = 0  # x of the DEST column, set by render_task
_textlines = []  # text row buffers, allocated after banner()
_dep_data = None  # departures, published by data_fetch_task
_time_set = False  # RTC was set from a response's Date header
_safe_to_fetch = asyncio.Event()  # render_task finished a cycle
_safe_to_fetch.set()
_disp_thread_stop = False
_disp_thread_lock = _thread.allocate_lock()  # held while display_thread runs
_disp_thread_crash = None  # backtrace text, written to flash by core 0
# with _FRAME_STATS: reports count, then average and maximum in us of
# clearing, drawing, update() and the frame period, set by display_thread
_frame_stats = [0] * 9

@micropython.native
def make_pen(color):
    """Return the pen of color, an (R, G, B) tuple, with the dimming
    applied: an RGB888 int, as PicoGraphics stores a pixel."""
    # unpacked by hand: tuple(<generator>) allocates
    r, g, b = color
    dimming = _dimming
    if dimming != 10:
        r = r * dimming // 10
        g = g * dimming // 10
        b = b * dimming // 10
    return r << 16 | g << 8 | b

def set_pen(disp, color):
    """Set pen with dimming applied. Color is (R, G, B) tuple."""
    disp.set_pen(make_pen(color))

# the pen of ETA, DEST and the ticker, updated with the dimming
_pen_bvg = make_pen(_BVG)

_console_y = 0

def console(*args, clear=False):
    global _console_y
    disp = _disp
    set_pen(disp, _RED)
    if clear:
        _console_y = 0
        disp.set_pen(0)
        disp.clear()
        set_pen(disp, _RED)
    str_args = [str(arg) for arg in args]
    s = " ".join(str_args)
    print(s)
    disp.text(s, 1, _console_y, scale=1)
    _h75.update(disp)
    _console_y += 6


def start_ap_mode():
    """Start WiFi Access Point for configuration"""
    ap = network.WLAN(network.AP_IF)

    # Disable station mode
    _wlan.active(False)

    # Enable AP mode
    ap.config(essid="BVGdisplay", security=0)  # Open network for easy setup
    ap.active(True)

    # Wait for AP to be active
    while not ap.active():
        time.sleep(0.1)

    ip = ap.ifconfig()[0]
    console("WiFi Setup Mode", clear=True)
    console("Connect to WiFi:")
    console("  BVGdisplay")
    console(f"Open: http://{ip}")


def network_connect(SSID, PSK):
    """Try to connect to WiFi. Returns True on success, False on failure."""
    for _ in range(6):
        _wlan.disconnect()
        while _wlan.active():
            _wlan.active(False)
        while not _wlan.active():
            _wlan.active(True)
        console(f"connecting to {SSID}...", clear=True)
        _wlan.config(pm=0xA11140)  # Turn WiFi power saving off for some slow APs
        _wlan.connect(SSID, PSK)

        for _ in range(10):
            status = _wlan.status()
            print("_wlan.status:", status)
            # negative: failed, 3: got an IP
            if status < 0 or status >= 3:
                break
            print("waiting for connection...")
            time.sleep(1)

        if _wlan.isconnected():
            print("connected")
            ip = _wlan.ifconfig()[0]
            console("IP:", ip)
            return True

        print("_wlan.status:", _wlan.status())
        console("Unable to connect.")
        console("Retrying...")
        time.sleep(2)
    console("Failed to connect.")
    return False


def ensure_internet_or_reset(host="1.1.1.1", port=80, timeout=60):
    """Blocking TCP connectivity test. Resets the device if it fails."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
    except Exception as e:
        crashlog.print_error("Connectivity test failed", e)
        console("No Internet. Restarting...")
        time.sleep(1)
        machine.reset()

def parse_iso_to_epoch(date_str):
    # Split off timezone
    dt_part = date_str[:19]  # "2026-01-04T04:46:00"
    tz_part = date_str[19:]  # "+01:00"

    # Parse date and time
    year = int(dt_part[0:4])
    month = int(dt_part[5:7])
    day = int(dt_part[8:10])
    hour = int(dt_part[11:13])
    minute = int(dt_part[14:16])
    second = int(dt_part[17:19])

    # Parse timezone offset ("Z" or none means UTC)
    if not tz_part or tz_part[0] == "Z":
        tz_offset = 0
    else:
        tz_sign = 1 if tz_part[0] == "+" else -1
        tz_hours = int(tz_part[1:3])
        tz_mins = int(tz_part[4:6])
        tz_offset = tz_sign * (tz_hours * 3600 + tz_mins * 60)

    # Convert to epoch (UTC)
    # mktime expects: (year, month, day, hour, min, sec, weekday, yearday)
    timestamp = time.mktime((year, month, day, hour, minute, second, 0, 0))

    # Adjust for timezone (subtract to convert to UTC)
    return timestamp - tz_offset


def parse_http_date(date_str):
    """Parse "Mon, 05 Jan 2026 19:17:30 GMT" into an RTC.datetime() tuple:
    (year, month, day, weekday, hours, minutes, seconds, subseconds)"""
    parts = date_str.split()
    day = int(parts[1])
    month = "JanFebMarAprMayJunJulAugSepOctNovDec".index(parts[2])
    if len(parts[2]) != 3 or month % 3:
        raise ValueError("bad month: " + parts[2])
    month = month // 3 + 1
    year = int(parts[3])
    hour, minute, second = map(int, parts[4].split(":"))
    return (year, month, day, 0, hour, minute, second, 0)


@micropython.native
def banner():
    import pngdec
    import random
    import assetfiles
    disp, disp_mv = _disp, _disp_mv
    pos_y = _disp_height // 2 - 16
    img = picographics.PicoGraphics(width=128, height=64, display=picographics.DISPLAY_GENERIC,
                                    pen_type=picographics.PEN_RGB888)
    img_mv = memoryview(img)
    png = pngdec.PNG(img)
    assetfiles.open_png(png, "assets/ansiwen128x64.png")
    png.decode(0, 0)
    bvg = picographics.PicoGraphics(display=hw_conf.DISPLAY)
    bvg_mv = memoryview(bvg)
    png2 = pngdec.PNG(bvg)
    assetfiles.open_png(png2, "assets/bvgdisp_logo.png")
    png2.decode(19, pos_y)
    l = _disp_width*4
    h75 = _h75
    for y in range(33):
        disp_mv[l*pos_y:l*(pos_y+32)] = img_mv[l*y:l*(y+32)]
        h75.update(disp)
        time.sleep_ms(20)
    time.sleep_ms(200)
    h75.clear()
    for i in range(0,100, 2):
        if random.randint(0, 100)<i:
            h75.update(bvg)
        else:
            h75.update(disp)
        time.sleep_ms(30)
    time.sleep_ms(700)
    blackline = b'\x00' * l
    for i in range(16):
        time.sleep_ms(10)
        bvg_mv[l*(pos_y+i):l*(pos_y+i+1)] = blackline
        bvg_mv[l*(pos_y+31-i):l*(pos_y+32-i)] = blackline
        h75.update(bvg)
    pos_y += 16
    bvg_mv[l*pos_y:l*(pos_y+1)] = b'\xFF\xFF\xFF\x00' * _disp_width
    h75.update(bvg)
    for i in range(64):
        time.sleep_ms(3)
        h75.set_pixel(i, pos_y, 0, 0, 0)
        h75.set_pixel(127 - i, pos_y, 0, 0, 0)


_type_colors = {
    "tram": (190, 20, 20),
    "regional": (190, 20, 20),
    "bus": (149, 39, 110),
    "suburban": (0, 141, 79),
    "subway": (17, 93, 145),
}

_subway_colors = {
    "U1": (125, 173, 76),
    "U2": (218, 66, 30),
    "U3": (0, 122, 91),
    "U4": (240, 215, 34),
    "U5": (126, 83, 48),
    "U55": (126, 83, 48),
    "U6": (140, 109, 171),
    "U7": (82, 141, 186),
    "U8": (34, 79, 134),
    "U9": (243, 121, 29),
}

def typ2col(t, l, subcol):
    """Return color tuple for transport type and line"""
    if t == "subway" and subcol:
        color = _subway_colors.get(l)
        if color:
            return color
        print("Unknown subway:", l)
    color = _type_colors.get(t)
    if color is None:
        print("Unknown type:", t)
        return _WHITE
    return color

@micropython.native
def render_text(s, buf=None, x=0, bold=False, clip=None, kerning=False):
    """Draw s into the text buffer buf at x, up to x = clip, and return its
    width. With buf=None, only measure it."""
    if not s:
        return 0
    stride = 0
    if buf:
        stride = (len(buf) - 4) >> 3  # see make_textbuf()
    if not clip:
        clip = stride * 8 if buf else 0xffff
    cursor_x = x
    # the font's tables, see font_bvg.py
    glyphs = font_bvg.GLYPHS
    glyph_size = font_bvg.GLYPH_SIZE
    extra = font_bvg.EXTRA
    if bold:
        ascii_idx = font_bvg.BOLD_ASCII
        extra_idx = font_bvg.BOLD_EXTRA_GLYPHS
    else:
        ascii_idx = font_bvg.ASCII
        extra_idx = font_bvg.EXTRA_GLYPHS
    height = font_bvg.HEIGHT
    last_col = 0
    for char in s:
        if cursor_x >= clip:
            # invisible
            break
        g = ord(char) - 32
        if 0 <= g < 95:
            g = ascii_idx[g]
        else:
            g = extra.find(char)
            g = extra_idx[g] if g >= 0 else 0
        if not g:
            print("missing character:", char)
            continue
        g = (g - 1) * glyph_size  # the glyph's offset in glyphs
        width = glyphs[g] - 1
        if kerning:
            # one more pixel of space if the glyphs would touch: the left
            # column has a pixel in the same row as one in the last glyph's
            # right column, or in a row next to it
            if glyphs[g + 1] & (last_col | last_col << 1 | last_col >> 1):
                cursor_x += 1
            last_col = 0
        if cursor_x + width + 8 <= x:
            # invisible
            cursor_x += width
            continue
        if kerning:
            last_col = glyphs[g + 2]
        if buf:
            for row in range(height):
                r = g + 3 + 2 * row
                byte_val = glyphs[r] << 8 | glyphs[r + 1]
                line = row * stride
                for col in range(width):
                    px = cursor_x + col
                    if px >= clip:
                        # invisible
                        break
                    if px < x:
                        # invisible
                        continue
                    if byte_val & (0x1 << (width - col)):
                        buf[line + (px >> 3)] |= 1 << (px & 7)
        # Move cursor for next character
        cursor_x += width
        if not kerning:
            cursor_x += 1
    return cursor_x - x


# blit()'s parameters for blit_asm(), 8 words; only the display thread
# uses them
_blit_params = bytearray(32)

@micropython.asm_thumb
def blit_asm(r0, r1):
    # The inner loop of blit(). r0: _blit_params, r1: the pen. The words of
    # r0: 0 dest address of the current line, 1 src address of its first
    # word, 2 lines left, 3 shift right, 4 shift left (32 - shift), 5 the
    # length in pixels, 6 dest bytes per line, 7 src bytes per line.
    # Only Thumb-1 instructions, so this runs on the RP2040 (M0+) as well
    # as on the RP2350 (M33): no IT, conditional branches instead.
    label(LINE)
    ldr(r4, [r0, 0])      # r4: dest of the current word
    ldr(r3, [r0, 4])      # r3: src word
    ldr(r2, [r0, 20])     # r2: pixels left
    cmp(r2, 0)
    ble(NEXTLINE)
    label(WORD)
    ldr(r5, [r3, 0])      # r5: the next 32 pixels, bit 0 the leftmost
    ldr(r7, [r0, 12])
    lsr(r5, r7)
    ldr(r6, [r3, 4])
    ldr(r7, [r0, 16])
    lsl(r6, r7)           # a shift by 32 gives 0
    orr(r5, r6)
    cmp(r2, 32)
    bge(FULL)
    mov(r7, 1)            # the last word: only r2 pixels
    lsl(r7, r2)
    sub(r7, 1)
    and_(r5, r7)
    label(FULL)
    mov(r6, r4)           # r6: dest of the current 8 pixels
    cmp(r5, 0)
    beq(NEXTWORD)
    label(GROUP)
    lsl(r7, r5, 24)       # the next 8 pixels empty?
    beq(EMPTY)
    # each pixel: its bit into the carry flag, then store the pen if set
    lsr(r5, r5, 1)
    bcc(P0)
    str(r1, [r6, 0])
    label(P0)
    lsr(r5, r5, 1)
    bcc(P1)
    str(r1, [r6, 4])
    label(P1)
    lsr(r5, r5, 1)
    bcc(P2)
    str(r1, [r6, 8])
    label(P2)
    lsr(r5, r5, 1)
    bcc(P3)
    str(r1, [r6, 12])
    label(P3)
    lsr(r5, r5, 1)
    bcc(P4)
    str(r1, [r6, 16])
    label(P4)
    lsr(r5, r5, 1)
    bcc(P5)
    str(r1, [r6, 20])
    label(P5)
    lsr(r5, r5, 1)
    bcc(P6)
    str(r1, [r6, 24])
    label(P6)
    lsr(r5, r5, 1)
    bcc(P7)
    str(r1, [r6, 28])
    label(P7)
    b(GNEXT)
    label(EMPTY)
    lsr(r5, r5, 8)
    label(GNEXT)
    add(r6, 32)
    cmp(r5, 0)
    bne(GROUP)
    label(NEXTWORD)
    add(r3, 4)
    add(r4, 128)
    sub(r2, 32)
    bgt(WORD)
    label(NEXTLINE)
    ldr(r4, [r0, 0])
    ldr(r7, [r0, 24])
    add(r4, r4, r7)
    str(r4, [r0, 0])
    ldr(r3, [r0, 4])
    ldr(r7, [r0, 28])
    add(r3, r3, r7)
    str(r3, [r0, 4])
    ldr(r7, [r0, 8])
    sub(r7, 1)
    str(r7, [r0, 8])
    bne(LINE)

@micropython.viper
def blit(dest: ptr32, src: ptr32, pen: uint, dst_off: int, dst_width: int, src_off: int, src_width: int, length: int):
    """Draw 8 lines of a text buffer (see make_textbuf()) into the frame
    buffer, in pen. Only the lit pixels are written: the display thread
    clears the frame buffer for every frame. Call it only from the display
    thread, which owns _blit_params.

    The work is done by blit_asm(), hand-written for the display thread's
    frame budget: it reads 32 pixels at once, skips every empty group of 8
    pixels with one test, and spends 3 instructions on a pixel. Check
    changes to it in an emulator or on both MCUs: the unix port can't run
    it.

    Offsets and lengths are in pixels. src_width is the text buffer's
    width, a multiple of 32. The source is read in whole words, up to one
    word past the region (the padding of make_textbuf()). There are no
    bounds checks: callers must keep offset + length within the row width
    of both buffers for all 8 rows. Out-of-range values silently corrupt
    memory, so fix display glitches in the callers' arithmetic, not here.
    """
    p = ptr32(_blit_params)
    p[0] = uint(dest) + uint(dst_off) * 4
    p[1] = uint(src) + uint(src_off >> 5) * 4
    p[2] = 8
    p[3] = src_off & 31
    p[4] = 32 - (src_off & 31)
    p[5] = length
    p[6] = dst_width * 4
    p[7] = src_width >> 3
    blit_asm(_blit_params, pen)

@micropython.viper
def clear_cols(buf: ptr8, stride: int, x: int, w: int):
    """Clear the pixels x to x + w - 1 in all 8 lines of a text buffer with
    stride bytes per line (see make_textbuf())."""
    end = x + w
    line = 0
    while line < 8 * stride:
        i = x
        while i < end and (i & 7):
            buf[line + (i >> 3)] &= 0xFF ^ (1 << (i & 7))
            i += 1
        while i + 8 <= end:
            buf[line + (i >> 3)] = 0
            i += 8
        while i < end:
            buf[line + (i >> 3)] &= 0xFF ^ (1 << (i & 7))
            i += 1
        line += stride

def set_warn_msg(msg):
    """Show msg in the warning ticker ("" hides it), if it changed."""
    global _warn_msg
    if _warn_msg == msg:
        return
    _warn_msg = msg
    render_warn_msg()

def render_warn_msg():
    """Render the current warning into the ticker buffer."""
    global _warn_msg_sz
    msg = _warn_msg
    warn_buf = _warn_buf
    # hide the ticker while it's re-rendered (or gone)
    _warn_msg_sz = 0
    if not msg:
        return
    msg_size = render_text(msg, None, kerning=True, clip=None)
    if msg_size > _WARN_BUF_SZ:
        print("WARNING: warn message too large:", msg_size)
    with _warn_buf_lock:
        clear_cols(warn_buf, _WARN_BUF_SZ // 8, 0, _WARN_BUF_SZ)
        render_text(msg, warn_buf, 0, kerning=True, clip=_WARN_BUF_SZ)
        _warn_msg_sz = min(msg_size, _WARN_BUF_SZ)
    if __debug__:
        print("updated warn message")

_separator = " - "
_separator_sz = render_text(_separator, kerning=True)

@micropython.native
def display_thread():
    global _disp_thread_crash
    print("display_thread started")
    warn_buf = _warn_buf
    warn_buf_lock = _warn_buf_lock
    _disp_thread_lock.acquire()
    try:
        # local copies of global vars to avoid lookup
        h75 = _h75
        spwm = _spwm
        disp = _disp
        disp_mv = _disp_mv
        disp_width = _disp_width
        row_y0 = _row_y0
        row_h = _row_height
        textlines = _textlines
        n_textlines = _n_textlines
        warn_y = row_y0 + (row_h * (n_textlines-1))
        separator_sz = _separator_sz
        # ticks_ms() wraps at 2**30 ms (~12.4 days): only compare via ticks_diff
        ticks_ms = time.ticks_ms
        ticks_add = time.ticks_add
        ticks_diff = time.ticks_diff
        if _FRAME_STATS:
            ticks_us = time.ticks_us
            stats = _frame_stats
            st_n = st_clear = st_draw = st_upd = st_per = 0
            mx_clear = mx_draw = mx_upd = mx_per = 0
            t_prev = ticks_us()
        t1 = ticks_ms()
        t_blink = t1
        t_warn = t1
        warn_show = False
        warn_x = 0
        blink_hide = False
        dest_off = 0
        disp.set_pen(0)
        while not _disp_thread_stop:
            if _FRAME_STATS:
                t_frame = ticks_us()
            n = n_textlines
            disp.clear()  # blit() only draws the lit pixels
            if _FRAME_STATS:
                t_cleared = ticks_us()
            pen = _pen_bvg
            dest_off = _dest_offset
            line_width = dest_off - _COL_GAP
            dest_width = disp_width - line_width - _ETA_WIDTH - 2*_COL_GAP
            warn_dt = ticks_diff(t1, t_warn)
            if warn_dt > _WARN_PAUSE_MS:
                if warn_dt > _WARN_CYCLE_MS:
                    warn_show = False
                    t_warn = ticks_add(t_warn, _WARN_CYCLE_MS)
                else:
                    warn_show = True
            # don't block: render_warn_msg() holds the lock while rendering
            # the whole message; draw the normal last row meanwhile
            if warn_show and _warn_msg_sz and warn_buf_lock.acquire(False):
                warn_msg_sz = _warn_msg_sz
                if warn_x >= warn_msg_sz:
                    warn_x = 0
                if warn_x > warn_msg_sz-disp_width:
                    blit(disp_mv, warn_buf, pen, warn_y*disp_width, disp_width, warn_x, _WARN_BUF_SZ, warn_msg_sz-warn_x)
                    blit(disp_mv, warn_buf, pen, warn_y*disp_width+warn_msg_sz-warn_x, disp_width, 0, _WARN_BUF_SZ, disp_width-warn_msg_sz+warn_x)
                else:
                    blit(disp_mv, warn_buf, pen, warn_y*disp_width, disp_width, warn_x, _WARN_BUF_SZ, disp_width)
                warn_buf_lock.release()
                warn_x += 1
                n = n_textlines - 1
            for i in range(n):
                buf, locks, states = textlines[i]
                row_y = (row_y0 + i * row_h)
                row_offset = row_y * disp_width
                # ETA column
                if locks[_S_ETA].acquire(False):
                    blit(disp_mv, buf, pen, row_offset + disp_width - _ETA_WIDTH, disp_width, 0, _TEXT_BUF_SZ, _ETA_WIDTH)
                    locks[_S_ETA].release()
                # LINE column
                if locks[_S_LINE].acquire(False):
                    blit(disp_mv, buf, states[_S_LINE_PEN], row_offset, disp_width, _ETA_WIDTH, _TEXT_BUF_SZ, line_width)
                    locks[_S_LINE].release()
                # DEST column
                if locks[_S_DEST].acquire(False):
                    dest_sz = states[_S_DEST_SZ]
                    show = True
                    if states[_S_BLINKING] and blink_hide:
                        show = False
                    if dest_sz > dest_width:
                        dest_sz += min(separator_sz, _TEXT_BUF_SZ - _ETA_WIDTH - line_width - dest_sz)
                        x = states[_S_DEST_X]
                        if x >= dest_sz:
                            # guard against a stale offset from a longer dest:
                            # it would make the blit length negative
                            x = _DEST_SCROLL_INIT
                        next_x = x+1
                        if next_x >= dest_sz:
                            next_x = _DEST_SCROLL_INIT
                        states[_S_DEST_X] = next_x
                        if x < 0:
                            x = 0
                        if show:
                            if x > dest_sz-dest_width:
                                blit(disp_mv, buf, pen, row_offset + dest_off, disp_width, _ETA_WIDTH+line_width+x, _TEXT_BUF_SZ, dest_sz-x)
                                blit(disp_mv, buf, pen, row_offset + dest_off + dest_sz - x, disp_width, _ETA_WIDTH+line_width, _TEXT_BUF_SZ, dest_width-dest_sz+x)
                            else:
                                blit(disp_mv, buf, pen, row_offset + dest_off, disp_width, _ETA_WIDTH+line_width+x, _TEXT_BUF_SZ, dest_width)
                    elif show:
                        blit(disp_mv, buf, pen, row_offset + dest_off, disp_width, _ETA_WIDTH+line_width, _TEXT_BUF_SZ, dest_sz)
                    locks[_S_DEST].release()
            if ticks_diff(t1, t_blink) > 0:
                t_blink = ticks_add(t_blink, _BLINK_DELAY_MS)
                blink_hide = not blink_hide
            if _FRAME_STATS:
                t_drawn = ticks_us()
            t1 = ticks_add(t1, _FRAME_DELAY_MS)
            if spwm:
                # waits for the panel refresh, so every frame is shown for
                # exactly _FRAME_DELAY_MS, and t1 keeps up with it
                if _FRAME_STATS:
                    t_upd = ticks_us()
                h75.update(disp, _SPWM_SYNC)
            else:
                # busy loop on core 2 to avoid flickering
                while True:
                    if ticks_diff(ticks_ms(), t1) >= 0:
                        break
                if _FRAME_STATS:
                    t_upd = ticks_us()
                h75.update(disp)
            if _FRAME_STATS:
                # only small ints: this must not allocate either
                d = ticks_diff(t_cleared, t_frame)
                st_clear += d
                if d > mx_clear:
                    mx_clear = d
                d = ticks_diff(t_drawn, t_cleared)
                st_draw += d
                if d > mx_draw:
                    mx_draw = d
                d = ticks_diff(ticks_us(), t_upd)
                st_upd += d
                if d > mx_upd:
                    mx_upd = d
                d = ticks_diff(t_frame, t_prev)
                t_prev = t_frame
                st_per += d
                if d > mx_per:
                    mx_per = d
                st_n += 1
                if st_n == _FRAME_STATS_N:
                    stats[1] = st_clear // st_n
                    stats[2] = mx_clear
                    stats[3] = st_draw // st_n
                    stats[4] = mx_draw
                    stats[5] = st_upd // st_n
                    stats[6] = mx_upd
                    stats[7] = st_per // st_n
                    stats[8] = mx_per
                    stats[0] += 1  # last: the render task prints on a change
                    st_n = st_clear = st_draw = st_upd = st_per = 0
                    mx_clear = mx_draw = mx_upd = mx_per = 0
    except Exception as e:
        print(f"display thread failed: {e}")
        _disp_thread_crash = crashlog.format_exception("display_thread", e)
        print(_disp_thread_crash)
    finally:
        if __debug__:
            print("display thread finished")
        _disp_thread_lock.release()

def write_disp_thread_crash():
    global _disp_thread_crash
    text = _disp_thread_crash
    _disp_thread_crash = None
    if text:
        crashlog.write(text)

def asyncio_exception_handler(loop, context):
    e = context.get("exception")
    if e is None:
        print("asyncio error:", context.get("message"))
        return
    crashlog.log_exception("task " + str(context.get("future")), e)

# states of a textline that is blank. The ETA is kept in minutes, as an
# int, so that its text is only built when it changes.
_empty_states = [None, "", "", 0, 0, False, 0, 0]

# render_row() and clear_row() are native: as bytecode, their frames are
# too big for the C stack (over 11 words), so every call would allocate
# them on the heap, which can trigger a gc.collect() on a full heap
@micropython.native
def render_row(i, eta, line, line_size, typ, dest, bg_col, line_width, force, colored, sub_colors):
    """Redraw the changed columns of text row i, each under its own lock.
    eta is in minutes, 0 for "now" (blinking, without text). line_size is
    the width of the line name. Returns whether DEST was redrawn."""
    tl_buf, locks, states = _textlines[i]

    # render ETA at beginning of textline buffer
    if eta != states[_S_ETA] or force:
        eta_s = "%d'" % eta if eta else ""
        eta_size = render_text(eta_s)
        with locks[_S_ETA]:
            clear_cols(tl_buf, _TEXT_BUF_SZ // 8, 0, _ETA_WIDTH)
            if eta_s:
                render_text(eta_s, tl_buf, _ETA_WIDTH - eta_size + 1, clip=_ETA_WIDTH)
            states[_S_ETA] = eta
            states[_S_BLINKING] = not eta
            if __debug__:
                print("updated ETA of line", i)

    # then render LINE
    if line != states[_S_LINE] or force:
        line_offset = max(line_width - line_size, 0)
        if sub_colors and bg_col:
            color = bg_col
        elif colored:
            color = typ2col(typ, line, sub_colors)
        else:
            color = _BVG
        with locks[_S_LINE]:
            clear_cols(tl_buf, _TEXT_BUF_SZ // 8, _ETA_WIDTH, line_width)
            render_text(line, tl_buf, _ETA_WIDTH + line_offset, bold=True, clip=_ETA_WIDTH+line_width, kerning=True)
            states[_S_LINE] = line
            states[_S_LINE_SZ] = line_size
            states[_S_LINE_PEN] = make_pen(color)
            if __debug__:
                print("updated LINE of line", i)

    # last is DEST, because it has flexible length
    if dest != states[_S_DEST] or force:
        with locks[_S_DEST]:
            clear_cols(tl_buf, _TEXT_BUF_SZ // 8, _ETA_WIDTH + line_width, _TEXT_BUF_SZ - _ETA_WIDTH - line_width)
            states[_S_DEST_SZ] = render_text(dest + _separator, tl_buf, _ETA_WIDTH + line_width, clip=_TEXT_BUF_SZ, kerning=True) - _separator_sz
            states[_S_DEST] = dest
            # reset now, so the display thread never combines the
            # new dest size with the old scroll offset; render_task
            # resets all updated rows again to scroll in sync
            states[_S_DEST_X] = _DEST_SCROLL_INIT
            if __debug__:
                print("updated DEST of line", i)
        return True
    return False

@micropython.native
def clear_row(i):
    """Blank text row i, unless it is blank already."""
    tl_buf, locks, states = _textlines[i]
    if states == _empty_states:
        return
    for lock in locks:
        lock.acquire()
    clear_cols(tl_buf, _TEXT_BUF_SZ // 8, 0, _TEXT_BUF_SZ)
    states[:] = _empty_states
    for lock in locks:
        lock.release()

async def render_task():
    global _dest_offset
    print("render task started")
    await asyncio.sleep(5) # give a chance to read the IP address
    colored = False
    sub_colors = False
    print("waiting for first data")
    console("waiting for data...")
    while _dep_data == None:
        await asyncio.sleep_ms(100)
    _thread.start_new_thread(display_thread, ())
    _safe_to_fetch.clear()
    t1 = time.ticks_ms()
    line_size_max = _LINE_MIN_WIDTH
    dim = 10
    stats_seen = 0
    while True:
        if _disp_thread_crash:
            write_disp_thread_crash()
        if _FRAME_STATS and _frame_stats[0] != stats_seen:
            st = _frame_stats
            stats_seen = st[0]
            print("frame us avg/max: clear", st[1], st[2], "draw", st[3], st[4],
                  "update", st[5], st[6], "period", st[7], st[8])
        try:
            data = _dep_data
            now = time.time()
            walk_delay = settings.get("WALK_DELAY")
            new_colored = settings.get("COLORED")
            new_sub_colors = settings.get("SUBWAY_COLORS")
            force_update = (new_colored != colored or new_sub_colors != sub_colors
                            or _dimming != dim)
            colored, sub_colors, dim = new_colored, new_sub_colors, _dimming
            if settings.check():
                line_size_max = _LINE_MIN_WIDTH
            dest_offset = line_size_max + _COL_GAP
            if _dest_offset != dest_offset:
                _dest_offset = dest_offset
                force_update = True
            line_width = line_size_max
            i = 0
            dest_redrawn = 0  # bit i: the DEST of row i was redrawn
            for line, typ, dest, when, bg_col in data:
                if i >= _n_textlines:
                    break

                eta_n = when - now + _ETA_ROUND_S
                if eta_n < walk_delay:
                    continue

                # 0 is "now"; below that only with a negative WALK_DELAY
                eta_n = max(eta_n // 60, 0)

                # measured only when the row's line name changes
                states = _textlines[i][2]
                if line == states[_S_LINE]:
                    line_size = states[_S_LINE_SZ]
                else:
                    line_size = render_text(line, bold=True, kerning=True)
                if render_row(i, eta_n, line, line_size, typ, dest,
                              bg_col, line_width, force_update, colored, sub_colors):
                    dest_redrawn |= 1 << i
                line_size_max = max(line_size_max, line_size)
                i += 1

            # restart the scrolling of all updated rows together
            for j in range(i):
                if dest_redrawn & (1 << j):
                    _textlines[j][1][_S_DEST].acquire()
                    _textlines[j][2][_S_DEST_X] = _DEST_SCROLL_INIT
            for j in range(i):
                if dest_redrawn & (1 << j):
                    _textlines[j][1][_S_DEST].release()

            for j in range(i, _n_textlines):
                clear_row(j)

        except Exception as e:
            crashlog.print_error("render task failed", e)
            print("last data:", data)

        # Signal fetch task: "I'm done, safe to run now"
        _safe_to_fetch.set()  # Wake up fetch task
        await asyncio.sleep_ms(0) # Yield to let fetch see signal
        _safe_to_fetch.clear()  # Clear immediately (edge-trigger)
        t1 = time.ticks_add(t1, _RENDER_CYCLE_MS)
        delay = time.ticks_diff(t1, time.ticks_ms())
        if delay > 0:
            await asyncio.sleep_ms(delay)
        else:
            print("render_task took too long:", delay)
            t1 = time.ticks_add(t1, -delay)

def parse_color(s):
    if not s:
        return None
    s = s[1:]  # strip '#'
    if len(s) == 3:
        r, g, b = (int(c, 16) * 0x11 for c in s)
    else:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return (r, g, b)

def clean_name(s):
    """Strip station name noise that wastes space on the panel."""
    s = s.replace(" [Endstelle]", "")
    s = s.replace(" (Berlin)", "")
    s = s.replace("  ", " ")
    return s.strip()

def set_clock(date):
    """Set the RTC from the value of a Date header. Returns True on the
    first call, when the clock wasn't set before."""
    global _time_set
    if not date:
        raise ValueError("response has no Date header")
    date = parse_http_date(date)
    old_t = time.time()
    _rtc.datetime(date)
    if not _time_set:
        _time_set = True
        if __debug__:
            print("time set")
        return True
    if __debug__:
        diff = time.time() - old_t
        if diff < -1 or diff > 1:
            print("time diff:", diff)
    return False

def parse_departures(deps, filtered):
    """Return (line, typ, dest, when, bg) tuples, skipping bad entries."""
    new_data = []
    for dep in deps:
        try:
            when = dep["when"]
            if not when:
                continue
            when = parse_iso_to_epoch(when)
            line_obj = dep["line"]
            line = line_obj["name"]
            if line in filtered:
                continue
            typ = line_obj["product"]
            dest = clean_name(dep["direction"])
            bg = parse_color((line_obj.get("color") or {}).get("bg"))
            new_data.append((line, typ, dest, when, bg))
        except Exception as e:
            # skip just this one, e.g. "direction": null
            print("skipping bad departure:", e)
    return new_data

def pick_warning(warnings, now):
    """Return the ticker text of the most important currently valid
    warning, or "" if there is none."""
    warn_msg = ""
    prio_min = 100
    for warn in warnings:
        try:
            prio = warn["priority"]
            # missing or null validity means open-ended
            valid_from = warn.get("validFrom")
            valid_until = warn.get("validUntil")
            if (prio >= prio_min
                    and (not valid_from or now >= parse_iso_to_epoch(valid_from))
                    and (not valid_until or now <= parse_iso_to_epoch(valid_until))):
                summary = warn["summary"]
                text = clean_name(warn["text"].split("\n")[0])
                warn_msg = f'{summary}: {text} *** '
                prio_min = prio+1
        except Exception as e:
            print("skipping bad warning:", e)
    return warn_msg

@micropython.viper
def find_bytes(buf: ptr8, start: int, end: int, pat: ptr8, pat_len: int) -> int:
    """Return the index of pat in buf[start:end], or -1. Upper case letters
    in buf also match lower case ones in pat, so pat must be lower case."""
    last = end - pat_len
    i = start
    while i <= last:
        j = 0
        while j < pat_len:
            c = buf[i + j]
            if c >= 65 and c <= 90:  # A-Z
                c |= 32
            if c != pat[j]:
                break
            j += 1
        if j == pat_len:
            return i
        i += 1
    return -1

def make_request():
    """Return the departures request for http_get(), from the settings:
    (host, port, ssl, request head, request tail)"""
    url = f'{settings.get("API_URL")}/stops/{settings.get("STATION_ID")}/departures'
    # API_URL can have a port and a path, eg. "http://host:8080/vbb"
    proto, _, host, path = url.split("/", 3)
    if proto == "http:":
        port, ssl = 80, None
    elif proto == "https:":
        port, ssl = 443, True
    else:
        raise ValueError("unsupported protocol: " + proto)
    hostname = host
    if ":" in host:
        hostname, port = host.split(":", 1)
        port = int(port)
    # pretty=false: whitespace would take up room in _recv_buf
    query = "results=14&duration=60&pretty=false&sparse=true&language=de"
    for product in ("bus", "tram", "subway", "regional", "suburban", "ferry", "express"):
        if not settings.get("SHOW_" + product.upper()):
            query += "&" + product + "=false"
    # HTTP/1.0, so the server can't send a chunked body
    return (hostname, port, ssl, f"GET /{path}?{query}".encode(),
            f" HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\nUser-Agent: compat\r\n\r\n".encode())

async def http_get(request, when):
    """Send a request from make_request(), with &when=<when> unless when
    is None, and read the whole response into _recv_buf. Returns its
    length. There is no timeout: wrap it in asyncio.wait_for()."""
    host, port, ssl, head, tail = request
    mv = memoryview(_recv_buf)
    n = 0
    stream, _ = await asyncio.open_connection(host, port, ssl=ssl)
    try:
        stream.write(head)
        if when is not None:
            stream.write(b"&when=%d" % when)
        stream.write(tail)
        await stream.drain()
        while True:
            if n == _RECV_BUF_SZ:
                raise ValueError("response larger than %d bytes" % _RECV_BUF_SZ)
            # Stream.read() would copy the growing response for every
            # TCP segment, and need up to twice its size in one piece
            r = await stream.readinto(mv[n:])
            if r is None:
                continue  # eg. only part of a TLS record arrived
            if not r:
                return n
            n += r
    finally:
        # also on a timeout: wait_for() cancels us
        await stream.wait_closed()  # closes the socket

def http_status(hdr_end):
    """Return the status code of the response in _recv_buf"""
    buf = _recv_buf
    i = find_bytes(buf, 0, hdr_end, b" ", 1) + 1  # after "HTTP/1.1 "
    if i == 0 or i + 3 > hdr_end:
        raise ValueError("bad status line")
    return (buf[i] - 48) * 100 + (buf[i + 1] - 48) * 10 + buf[i + 2] - 48

def http_header(name, hdr_end):
    """Return the value of a header of the response in _recv_buf as str, or
    None. name is lower case, with the CRLF before it and the colon, eg.
    b"\\r\\ndate:": the case of header names differs, eg. behind a proxy."""
    buf = _recv_buf
    i = find_bytes(buf, 0, hdr_end, name, len(name))
    if i < 0:
        return None
    i += len(name)
    while buf[i] == 32:  # " "
        i += 1
    # the CRLF at hdr_end ends the last header
    end = find_bytes(buf, i, hdr_end + 2, b"\r\n", 2)
    return str(memoryview(buf)[i:end], "ascii")

async def data_fetch_task():
    """Fetches data every 10 seconds"""
    print("fetch task started")

    async def fetch(request, when):
        """One request. Returns True if the next one should follow right away."""
        global _dep_data
        n = await http_get(request, when)
        hdr_end = find_bytes(_recv_buf, 0, n, b"\r\n\r\n", 4)
        if hdr_end < 0:
            raise ValueError("incomplete response headers")
        status = http_status(hdr_end)
        if status != 200:
            print("fetch failed:", status)
            return False
        if set_clock(http_header(b"\r\ndate:", hdr_end)):
            return True

        parse_start_ms = time.ticks_ms()
        # parses in place; the result holds copies, not slices of _recv_buf
        data = json.loads(memoryview(_recv_buf)[hdr_end + 4:n])
        if __debug__:
            print("parsing:", time.ticks_diff(time.ticks_ms(), parse_start_ms),
                  "ms,", n - hdr_end - 4, "bytes")

        _dep_data = parse_departures(data["departures"], settings.get("FILTERED"))
        # only some APIs send warnings, eg. v6.bvg.transport.rest doesn't
        warn_msg = pick_warning(data.get("warnings") or (), time.time())
        # compare by text, not id: the id can change while the
        # text stays the same (duplicate warnings), or vice versa
        set_warn_msg(warn_msg)
        if __debug__:
            print("updated data")
        return False

    request = None
    request_version = -1  # the settings.version() request was made for
    while True:
        try:
            await _safe_to_fetch.wait()
            gc.collect()
            # built only when the settings change, not for every fetch
            if request_version != settings.version():
                request = make_request()
                request_version = settings.version()
            when = None
            if _time_set:
                when = time.time() + int(settings.get("WALK_DELAY"))
            # http_get() has no timeout: without this, a server or link that
            # stops responding mid-request would hang the fetch task forever
            if await asyncio.wait_for(fetch(request, when), _FETCH_TIMEOUT):
                continue
        except asyncio.TimeoutError:
            print("fetch timed out")
        except Exception as e:
            crashlog.print_error("Fetch task failed", e)

        if __debug__:
            print("memfree:", gc.mem_free())
        await asyncio.sleep(_FETCH_DELAY)


async def check_night_time_task():
    """Check if current time is within night hours"""
    global _dimming, _pen_bvg
    while not _time_set:
        await asyncio.sleep(1)
    while True:
        if __debug__:
            print("check_night_time")
        now = _rtc.datetime()
        # bad settings (e.g. NIGHT_START "") must not end the task: an
        # exception escaping it stops the whole program
        try:
            # RTC is UTC, apply timezone offset
            tz_offset = settings.get("TIMEZONE") or 0
            # the check runs at second 55 (see the sleep below), so judge
            # by the coming minute; the % below wraps it into the day
            current_minutes = (now[4] + tz_offset) * 60 + now[5] + 1

            start = settings.get("NIGHT_START")
            end = settings.get("NIGHT_END")

            start_h, start_m = map(int, start.split(":"))
            end_h, end_m = map(int, end.split(":"))

            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            since_start = (current_minutes-start_minutes)%(24*60)
            since_end = (current_minutes-end_minutes)%(24*60)

            if since_start < since_end:
                dim = int(settings.get("NIGHT_DIMMING"))
            else:
                dim = 10

            if _dimming != dim:
                if __debug__:
                    print("changing dimming")
                _dimming = dim
                _pen_bvg = make_pen(_BVG)
        except Exception as e:
            crashlog.print_error("night time check failed", e)

        # wake up at the next second 55 that is at least 26 s away
        await asyncio.sleep(85 - (now[6] + 30) % 60)

# Main entry point
async def main():
    from web_server import start_web_server

    asyncio.get_event_loop().set_exception_handler(asyncio_exception_handler)

    # Check if WiFi credentials are configured
    ssid = settings.get("WIFI_SSID")
    ap_mode = not (ssid and ssid != "REPLACE_WITH_YOUR_SSID"
                   and network_connect(ssid, settings.get("WIFI_PASSWORD")))
    if ap_mode:
        start_ap_mode()
    else:
        ensure_internet_or_reset()
        console("connected to internet")

    # Start web server for settings configuration
    web_server = await start_web_server(port=80)

    if ap_mode:
        # In AP mode, only run the web server for configuration
        await asyncio.Event().wait()  # wait forever
    else:
        # Normal mode: run data fetcher and display
        fetcher = asyncio.create_task(data_fetch_task())
        renderer = asyncio.create_task(render_task())
        night_time_checker = asyncio.create_task(check_night_time_task())
        await asyncio.gather(fetcher, renderer, night_time_checker)

    web_server.close()


banner()

gc.collect()

# _textlines[row]: (buffer, locks, states); the buffer holds ETA|LINE|DEST
_textlines = [(make_textbuf(_TEXT_BUF_SZ),
               (_thread.allocate_lock(), _thread.allocate_lock(), _thread.allocate_lock()),
               _empty_states[:])
              for _ in range(_n_textlines)]
_warn_buf = make_textbuf(_WARN_BUF_SZ)
_warn_buf_lock = _thread.allocate_lock()
_warn_msg = ""
_warn_msg_sz = 0
# the HTTP response of a fetch, allocated once while the heap is still
# unfragmented, see http_get()
_recv_buf = bytearray(_RECV_BUF_SZ)

# Start the event loop
_crashed = False
try:
    asyncio.run(main())
except Exception as e:
    _crashed = True
    crashlog.log_exception("main", e)
    raise
finally:
    print("shutting down")
    # stop the display thread first, it might be waiting in _h75.update()
    _disp_thread_stop = True
    _disp_thread_lock.acquire()
    _disp_thread_lock.release()
    _h75.stop()
    write_disp_thread_crash()
    # only on a crash, not on Ctrl-C, so the REPL stays usable
    if _crashed and _RESET_ON_CRASH:
        print("resetting")
        machine.reset()
