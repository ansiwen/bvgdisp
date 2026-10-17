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

_TEXT_BUF_SZ = const(384)
_WARN_BUF_SZ = const(2560)
# a whole HTTP response, headers included. With the default 14 results,
# v6.bvg.transport.rest sends about 19 KB, vbb.a6n.de about 2 KB.
_RECV_BUF_SZ = const(32 * 1024)

_FRAME_RATE = const(30)
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
_BG = const(0)

_S_ETA = const(0)
_S_LINE = const(1)
_S_DEST = const(2)
_S_DEST_SZ = const(3)
_S_DEST_X = const(4)
_S_BLINKING = const(5)

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
    buf = picographics.PicoGraphics(width=sz, height=8, display=picographics.DISPLAY_GENERIC,
                                    pen_type=picographics.PEN_RGB888)
    buf.set_pen(0)
    buf.clear()
    return (buf, memoryview(buf))

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
_textlines = []  # text row buffers, allocated by render_task
_dep_data = None  # departures, published by data_fetch_task
_time_set = False  # RTC was set from a response's Date header
_safe_to_fetch = asyncio.Event()  # render_task finished a cycle
_safe_to_fetch.set()
_disp_thread_stop = False
_disp_thread_lock = _thread.allocate_lock()  # held while display_thread runs
_disp_thread_crash = None  # backtrace text, written to flash by core 0

@micropython.native
def set_pen(disp, color):
    """Set pen with dimming applied. Color is (R, G, B) tuple."""
    # unpacked by hand: tuple(<generator>) and create_pen(*color) allocate
    r, g, b = color
    dimming = _dimming
    if dimming != 10:
        r = r * dimming // 10
        g = g * dimming // 10
        b = b * dimming // 10
    disp.set_pen(disp.create_pen(r, g, b))

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

# the bold glyphs by their character, so that render_text() doesn't have
# to build "*" + char for every character
_font_bold = {k[1:]: v for k, v in font_bvg.font_small.items()
              if len(k) == 2 and k[0] == "*"}

@micropython.native
def render_text(s, disp=None, x=0, y=0, bold=False, clip=None, kerning=False):
    if not s:
        return 0
    if not clip:
        if disp:
            clip = disp.get_bounds()[0]
        else:
            clip = 0xffff
    cursor_x = x
    font = font_bvg.font_small
    font_bold = _font_bold
    height = font["fontheight"]
    last_col = 0
    for char in s:
        if cursor_x >= clip:
            # invisible
            break
        glyph = (bold and font_bold.get(char)) or font.get(char)
        if glyph is None:
            print("missing character:", char)
            continue
        width = glyph[0] - 1
        if kerning:
            for row in range(height):
                check = glyph[row + 1] >> width and ((last_col & (0b111<<row)) != 0)
                if check:
                    cursor_x += 1
                    break
            last_col = 0
        if cursor_x + width + 8 <= x:
            # invisible
            cursor_x += width
            continue
        # when only measuring, the rows matter just for kerning
        if disp or kerning:
            for row in range(height):
                byte_val = glyph[row + 1]
                if kerning:
                    last_col |= (byte_val&2)<<row
                if not disp:
                    continue
                for col in range(width):
                    px = cursor_x + col
                    if px >= clip:
                        # invisible
                        break
                    if px < x:
                        # invisible
                        continue
                    if byte_val & (0x1 << (width - col)):
                        disp.pixel(px, y + row)
        # Move cursor for next character
        cursor_x += width
        if not kerning:
            cursor_x += 1
    return cursor_x - x


@micropython.viper
def blit(dest: ptr32, src: ptr32, dst_off: uint, dst_width: uint, src_off: uint, src_width: uint, length: uint):
    """fast copy of 8 lines from a text buffer to the display buffer

    Hand-optimized for the display thread's frame budget: the 8x unrolled
    copy and the pointer arithmetic are deliberate. Don't restructure or
    "simplify" it without measuring frame times on the device.

    Offsets and lengths are in pixels (4 bytes each, RGB888). There are no
    bounds checks: callers must keep length >= 0 (a negative one becomes a
    huge uint) and keep offset + length within the row width of both
    buffers for all 8 rows. Out-of-range values silently corrupt memory,
    so fix display glitches in the callers' arithmetic, not here.
    """
    dest = ptr32(uint(dest) + dst_off*4)
    src = ptr32(uint(src) + src_off*4)
    src_stop = uint(0)
    l = uint(length & uint(0xFFFFFFF8))*4
    r = uint(length & uint(0x7))*4
    for n in range(8):
        src_stop = uint(src) + l
        while uint(src) < src_stop:
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
        src_stop:uint = uint(src) + r
        while uint(src) < src_stop:
            dest[0] = src[0]
            dest = ptr32(uint(dest) + 4)
            src = ptr32(uint(src) + 4)
        dest = ptr32(uint(dest) + (dst_width-length)*4)
        src = ptr32(uint(src) + (src_width-length)*4)

def set_warn_msg(msg):
    """Show msg in the warning ticker ("" hides it), if it changed."""
    global _warn_msg
    if _warn_msg == msg:
        return
    _warn_msg = msg
    render_warn_msg()

def render_warn_msg():
    """(Re-)render the current warning into the ticker buffer, e.g. after
    the dimming changed."""
    global _warn_msg_sz
    msg = _warn_msg
    warn_buf = _warn_buf[0]
    # hide the ticker while it's re-rendered (or gone)
    _warn_msg_sz = 0
    if not msg:
        return
    msg_size = render_text(msg, None, kerning=True, clip=None)
    if msg_size > _WARN_BUF_SZ:
        print("WARNING: warn message too large:", msg_size)
    with _warn_buf_lock:
        warn_buf.set_pen(_BG)
        warn_buf.clear()
        set_pen(warn_buf, _BVG)
        render_text(msg, warn_buf, 0, 0, kerning=True, clip=_WARN_BUF_SZ)
        _warn_msg_sz = min(msg_size, _WARN_BUF_SZ)
    print("updated warn message")

_separator = " - "
_separator_sz = render_text(_separator, kerning=True)

@micropython.native
def display_thread():
    global _disp_thread_crash
    print("display_thread started")
    warn_mv = _warn_buf[1]
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
        t1 = ticks_ms()
        t_blink = t1
        t_warn = t1
        warn_show = False
        warn_x = 0
        blink_hide = False
        dest_off = 0
        disp.set_pen(0)
        while not _disp_thread_stop:
            n = n_textlines
            disp.clear()
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
                    blit(disp_mv, warn_mv, warn_y*disp_width, disp_width, warn_x, _WARN_BUF_SZ, warn_msg_sz-warn_x)
                    blit(disp_mv, warn_mv, warn_y*disp_width+warn_msg_sz-warn_x, disp_width, 0, _WARN_BUF_SZ, disp_width-warn_msg_sz+warn_x)
                else:
                    blit(disp_mv, warn_mv, warn_y*disp_width, disp_width, warn_x, _WARN_BUF_SZ, disp_width)
                warn_buf_lock.release()
                warn_x += 1
                n = n_textlines - 1
            for i in range(n):
                buf, mv, locks, states = textlines[i]
                row_y = (row_y0 + i * row_h)
                row_offset = row_y * disp_width
                # ETA column
                if locks[_S_ETA].acquire(False):
                    blit(disp_mv, mv, row_offset + disp_width - _ETA_WIDTH, disp_width, 0, _TEXT_BUF_SZ, _ETA_WIDTH)
                    locks[_S_ETA].release()
                # LINE column
                if locks[_S_LINE].acquire(False):
                    blit(disp_mv, mv, row_offset, disp_width, _ETA_WIDTH, _TEXT_BUF_SZ, line_width)
                    locks[_S_LINE].release()
                # DEST column
                if locks[_S_DEST].acquire(False):
                    dest_sz = states[_S_DEST_SZ]
                    show = True
                    if states[_S_BLINKING] and blink_hide:
                        disp.rectangle(dest_off, row_y, dest_width, 8)
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
                                blit(disp_mv, mv, row_offset + dest_off, disp_width, _ETA_WIDTH+line_width+x, _TEXT_BUF_SZ, dest_sz-x)
                                blit(disp_mv, mv, row_offset + dest_off + dest_sz - x, disp_width, _ETA_WIDTH+line_width, _TEXT_BUF_SZ, dest_width-dest_sz+x)
                            else:
                                blit(disp_mv, mv, row_offset + dest_off, disp_width, _ETA_WIDTH+line_width+x, _TEXT_BUF_SZ, dest_width)
                    elif show:
                        disp.rectangle(dest_off, row_y, dest_width, 8)
                        blit(disp_mv, mv, row_offset + dest_off, disp_width, _ETA_WIDTH+line_width, _TEXT_BUF_SZ, dest_sz)
                    locks[_S_DEST].release()
            if ticks_diff(t1, t_blink) > 0:
                t_blink = ticks_add(t_blink, _BLINK_DELAY_MS)
                blink_hide = not blink_hide
            # elapsed = time.ticks_diff(time.ticks_ms(), t1)
            # print("elapsed", elapsed)
            t1 = ticks_add(t1, _FRAME_DELAY_MS)
            if spwm:
                # waits for the panel refresh, so every frame is shown for
                # exactly _FRAME_DELAY_MS, and t1 keeps up with it
                h75.update(disp, _SPWM_SYNC)
            else:
                # busy loop on core 2 to avoid flickering
                while True:
                    if ticks_diff(ticks_ms(), t1) >= 0:
                        break
                h75.update(disp)
    except Exception as e:
        print(f"display thread failed: {e}")
        _disp_thread_crash = crashlog.format_exception("display_thread", e)
        print(_disp_thread_crash)
    finally:
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

# states of a textline that is blank
_empty_states = ["", "", "", 0, 0, False]

# the ETA texts, so that render_task doesn't build them every second
_ETA_STRS_N = const(100)
_eta_strs = tuple(str(n) + "'" for n in range(_ETA_STRS_N))

def render_row(i, eta_s, blinking, line, line_size, typ, dest, bg_col, line_width, force, colored, sub_colors):
    """Redraw the changed columns of text row i, each under its own lock.
    line_size is the width of the line name. Returns whether DEST was
    redrawn."""
    tl_buf, _, locks, states = _textlines[i]

    # render ETA at beginning of textline buffer
    if eta_s != states[_S_ETA] or force:
        eta_size = render_text(eta_s)
        with locks[_S_ETA]:
            tl_buf.set_pen(_BG)
            tl_buf.rectangle(0, 0, _ETA_WIDTH, 8)
            if eta_s:
                set_pen(tl_buf, _BVG)
                render_text(eta_s, tl_buf, _ETA_WIDTH - eta_size + 1, 0, clip=_ETA_WIDTH)
            states[_S_ETA] = eta_s
            states[_S_BLINKING] = blinking
            print(f"updated ETA of line {i}")

    # then render LINE
    if line != states[_S_LINE] or force:
        line_offset = max(line_width - line_size, 0)
        with locks[_S_LINE]:
            tl_buf.set_pen(_BG)
            tl_buf.rectangle(_ETA_WIDTH, 0, line_width, 8)
            if sub_colors and bg_col:
                set_pen(tl_buf, bg_col)
            elif colored:
                set_pen(tl_buf, typ2col(typ, line, sub_colors))
            else:
                set_pen(tl_buf, _BVG)
            render_text(line, tl_buf, _ETA_WIDTH + line_offset, 0, bold=True, clip=_ETA_WIDTH+line_width, kerning=True)
            states[_S_LINE] = line
            print(f"updated LINE of line {i}")

    # last is DEST, because it has flexible length
    if dest != states[_S_DEST] or force:
        with locks[_S_DEST]:
            tl_buf.set_pen(_BG)
            tl_buf.rectangle(_ETA_WIDTH + line_width, 0, _TEXT_BUF_SZ - _ETA_WIDTH - line_width, 8)
            set_pen(tl_buf, _BVG)
            states[_S_DEST_SZ] = render_text(dest + _separator, tl_buf, _ETA_WIDTH + line_width, 0, clip=_TEXT_BUF_SZ - _ETA_WIDTH - line_width, kerning=True) - _separator_sz
            states[_S_DEST] = dest
            # reset now, so the display thread never combines the
            # new dest size with the old scroll offset; render_task
            # resets all updated rows again to scroll in sync
            states[_S_DEST_X] = _DEST_SCROLL_INIT
            print(f"updated DEST of line {i}")
        return True
    return False

def clear_row(i):
    """Blank text row i, unless it is blank already."""
    tl_buf, _, locks, states = _textlines[i]
    if states == _empty_states:
        return
    for lock in locks:
        lock.acquire()
    tl_buf.set_pen(_BG)
    tl_buf.clear()
    states[:] = _empty_states
    for lock in locks:
        lock.release()

async def render_task():
    global _dest_offset, _textlines
    print("render task started")
    await asyncio.sleep(5) # give a chance to read the IP address
    colored = False
    sub_colors = False
    print("waiting for first data")
    console("waiting for data...")
    while _dep_data == None:
        await asyncio.sleep_ms(100)
    gc.collect()
    # _textlines[row] structure: ETA|LINE|DEST
    _textlines = []
    for _ in range(_n_textlines):
        buf, mv = make_textbuf(_TEXT_BUF_SZ)
        _textlines.append((
            buf, mv,
            (_thread.allocate_lock(), _thread.allocate_lock(),_thread.allocate_lock()),
            _empty_states[:]
        ))

    _thread.start_new_thread(display_thread, ())
    _safe_to_fetch.clear()
    t1 = time.ticks_ms()
    line_size_max = _LINE_MIN_WIDTH
    dim = 10
    while True:
        if _disp_thread_crash:
            write_disp_thread_crash()
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

                eta_n //= 60

                blinking = False
                if eta_n < 1:
                    eta_s = ""
                    blinking = True
                elif eta_n < _ETA_STRS_N:
                    eta_s = _eta_strs[eta_n]
                else:
                    eta_s = str(eta_n) + "'"

                line_size = render_text(line, bold=True, kerning=True)
                if render_row(i, eta_s, blinking, line, line_size, typ, dest,
                              bg_col, line_width, force_update, colored, sub_colors):
                    dest_redrawn |= 1 << i
                line_size_max = max(line_size_max, line_size)
                i += 1

            # restart the scrolling of all updated rows together
            for j in range(i):
                if dest_redrawn & (1 << j):
                    _textlines[j][2][_S_DEST].acquire()
                    _textlines[j][3][_S_DEST_X] = _DEST_SCROLL_INIT
            for j in range(i):
                if dest_redrawn & (1 << j):
                    _textlines[j][2][_S_DEST].release()

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
        print("time set")
        return True
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
        print("parsing:", time.ticks_diff(time.ticks_ms(), parse_start_ms),
              "ms,", n - hdr_end - 4, "bytes")

        _dep_data = parse_departures(data["departures"], settings.get("FILTERED"))
        warn_msg = pick_warning(data["warnings"], time.time())
        # compare by text, not id: the id can change while the
        # text stays the same (duplicate warnings), or vice versa
        set_warn_msg(warn_msg)
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

        print("memfree:", gc.mem_free())
        await asyncio.sleep(_FETCH_DELAY)


async def check_night_time_task():
    """Check if current time is within night hours"""
    global _dimming
    while not _time_set:
        await asyncio.sleep(1)
    while True:
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
                print("changing dimming")
                _dimming = dim
                render_warn_msg()
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

# only now: the big ticker buffer needs the memory banner() just freed
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
