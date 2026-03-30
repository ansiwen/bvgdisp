import time
import socket
import machine
import network
import asyncio
import aiohttp
from hub75 import Hub75
from picographics import PicoGraphics, DISPLAY_GENERIC, PEN_RGB888
from font_bvg import font_small
import settings
import hw_conf
import gc
import _thread
import json

_ETA_WIDTH = const(11)

_TEXT_BUF_SZ = const(512)
_WARN_BUF_SZ = const(2560)
_COL_GAP = const(3)

_S_ETA = const(0)
_S_LINE = const(1)
_S_DEST = const(2)
_S_DEST_SZ = const(3)
_S_DEST_X = const(4)
_S_BLINKING = const(5)

_FRAME_RATE = const(25)
_DEST_SCROLL_DELAY = const(5)
_BLINK_DELAY_MS = const(600)
_FETCH_DELAY = const(10)
_WARN_DURATION = const(45)
_WARN_PAUSE = const(30)

_FRAME_DELAY = const(1000//_FRAME_RATE)
_DEST_SCROLL_INIT = const(-_FRAME_RATE*_DEST_SCROLL_DELAY)
_WARN_CYCLE = const(_WARN_DURATION+_WARN_PAUSE)
_LINE_MIN_WIDTH = const(13)

# Colors as tuples (R, G, B)
_RED = const((120, 0, 0))
_YELLOW = const((255, 180, 0))
_BVG = const((255, 170, 0))
_WHITE = const((255, 255, 255))
#_BG = _disp.create_pen(4,4,4)
_BG = const(0)


_rtc = machine.RTC()

# Enable the Wireless
network.country("DE")
network.hostname("BVGdisplay")

_wlan = network.WLAN(network.STA_IF)

# Setup for the display

def make_disp():
    disp = PicoGraphics(display=hw_conf.DISPLAY)
    mv = memoryview(disp)
    return disp, mv

def make_col(col_w):
    buf = PicoGraphics(width=col_w, height=8, display=DISPLAY_GENERIC, pen_type=PEN_RGB888)
    buf.set_pen(0)
    buf.clear()
    # (buf, buf_mv, lock, buf_width)
    return (buf, memoryview(buf))

_disp, _disp_mv = make_disp()
_disp.set_pen(0)
_disp.clear()
_disp_width, _disp_height = _disp.get_bounds()

_warn_y = 55 if _disp_height == 64 else 24
_row_y0 = 1 if _disp_height == 64 else 0
_row_height = 9 if _disp_height == 64 else 8
_n_textlines = (_warn_y - _row_y0) // _row_height + 1  # +1: last row shares the scroll position

_dest_offset = 0

_h75 = Hub75(_disp_width, _disp_height, color_order=hw_conf.COLOR_ORDER)
_h75.start()

_dimming = 10

@micropython.native
def set_pen(disp, color):
    """Set pen with dimming applied. Color is (R, G, B) tuple."""
    dimming = _dimming
    if dimming != 10:
        color = tuple(v * dimming // 10 for v in color)
    disp.set_pen(disp.create_pen(*color))

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
    print(f"AP Mode active. IP: {ip}")
    return ip


def network_connect(SSID, PSK):
    """Try to connect to WiFi. Returns True on success, False on failure."""
    for i in range(6):
        _wlan.disconnect()
        while _wlan.active():
            _wlan.active(False)
        while not _wlan.active():
            _wlan.active(True)
        # Sets the Wireless LED pulsing and attempts to connect to your local network.
        console(f"connecting to {SSID}...", clear=True)
        _wlan.config(pm=0xA11140)  # Turn WiFi power saving off for some slow APs
        _wlan.connect(SSID, PSK)

        for j in range(10):
            print("_wlan.status:", _wlan.status())
            if _wlan.status() < 0 or _wlan.status() >= 3:
                break
            print("waiting for connection...")
            time.sleep(1)

        # Handle connection error. Switches the Warn LED on.
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


def connectivity_test(host="1.1.1.1", port=80, timeout=60):
    """Minimal blocking TCP connectivity test. Returns True if connected."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except Exception as e:
        import sys

        print(f"Connectivity test failed: {e}")
        sys.print_exception(e)
        console("No Internet. Restarting...")
        time.sleep(1)
        machine.reset()

@micropython.native
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

    # Parse timezone offset
    tz_sign = 1 if tz_part[0] == "+" else -1
    tz_hours = int(tz_part[1:3])
    tz_mins = int(tz_part[4:6])
    tz_offset = tz_sign * (tz_hours * 3600 + tz_mins * 60)

    # Convert to epoch (UTC)
    # mktime expects: (year, month, day, hour, min, sec, weekday, yearday)
    timestamp = time.mktime((year, month, day, hour, minute, second, 0, 0))

    # Adjust for timezone (subtract to convert to UTC)
    return timestamp - tz_offset


@micropython.native
def parse_http_date(date_str):
    # "Mon, 05 Jan 2026 19:17:30 GMT"
    import time

    months = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }

    parts = date_str.split()
    day = int(parts[1])
    month = months[parts[2]]
    year = int(parts[3])

    hour, minute, second = map(int, parts[4].split(":"))

    # RTC.datetime() format: (year, month, day, weekday, hours, minutes, seconds, subseconds)
    timestamp = (year, month, day, 0, hour, minute, second, 0)

    return timestamp


@micropython.native
def banner():
    import pngdec
    import random
    disp, disp_mv = _disp, _disp_mv
    pos_y = _disp_height // 2 - 16
    img = PicoGraphics(width=128, height=64, display=DISPLAY_GENERIC, pen_type=PEN_RGB888)
    img_mv = memoryview(img)
    png = pngdec.PNG(img)
    png.open_file("ansiwen128x64.png")
    png.decode(0, 0)
    bvg = PicoGraphics(display=hw_conf.DISPLAY)
    bvg_mv = memoryview(bvg)
    png2 = pngdec.PNG(bvg)
    png2.open_file("bvg_logo_32.png")
    png2.decode(46, pos_y)
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


@micropython.native
def typ2col(t, l, subcol):
    """Return color tuple for transport type and line"""
    if t == "tram" or t == "regional":
        return (190, 20, 20)
    elif t == "bus":
        return (149, 39, 110)
    elif t == "suburban":
        return (0, 141, 79)
    elif t == "subway":
        if subcol:
            if l == "U1":
                return (125, 173, 76)
            elif l == "U2":
                return (218, 66, 30)
            elif l == "U3":
                return (0, 122, 91)
            elif l == "U4":
                return (240, 215, 34)
            elif l == "U5" or l == "U55":
                return (126, 83, 48)
            elif l == "U6":
                return (140, 109, 171)
            elif l == "U7":
                return (82, 141, 186)
            elif l == "U8":
                return (34, 79, 134)
            elif l == "U9":
                return (243, 121, 29)
            else:
                print("Unkown subway:", l)
        return (17, 93, 145)
    print("Unkown type:", t)
    return _WHITE

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
    font = font_small
    height = font["fontheight"]
    last_col = 0
    for char in s:
        if cursor_x >= clip:
            # invisible
            break
        if bold:
            glyph = font.get("*" + char)
        if not bold or glyph == None:
            glyph = font.get(char)
        if glyph is None:
            print("missing character:", char)
            continue
        width = glyph[0] - 1
        if kerning:
            for row in range(height):
                if bold:
                    check = glyph[row + 1] >> width and ((last_col & (0b10<<row)) != 0)
                else:
                    check = glyph[row + 1] >> width and ((last_col & (0b111<<row)) != 0)
                if check:
                    cursor_x += 1
                    break
            last_col = 0
        # if cursor_x + width > clip:
        #     # don't print partial character
        #     break
        if cursor_x + width + 8 <= x:
            # invisible
            cursor_x += width
            continue
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
    """fast copy of 8 lines from a text buffer to the display buffer"""
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

def warn_msg_update(msg=None):
    global _warn_msg, _warn_msg_sz
    warn_buf = _warn_buf[0]
    _warn_msg_sz = 0
    if msg == None:
        msg = _warn_msg
    else:
        if _warn_msg == msg:
            return
        _warn_msg = msg
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

_disp_thread_stop = False
_disp_thread_lock = _thread.allocate_lock()

_separator = " - "
_separator_sz = render_text(_separator, kerning=True)

@micropython.native
def display_thread():
    print("display_thread started")
    warn_mv = _warn_buf[1]
    warn_buf_lock = _warn_buf_lock
    _disp_thread_lock.acquire()
    try:
        # local copies of global vars to avoid lookup
        h75 = _h75
        disp = _disp
        disp_mv = _disp_mv
        disp_width = _disp_width
        warn_y = _warn_y
        row_y0 = _row_y0
        row_h = _row_height
        textlines = _textlines
        n_textlines = _n_textlines
        separator_sz = _separator_sz
        t1 = time.ticks_ms()
        t_blink = t1
        warn_x = 0
        warn_msg_sz = 0
        blink_hide = False
        while not _disp_thread_stop:
            dest_off = _dest_offset
            line_width = dest_off - _COL_GAP
            dest_width = disp_width - line_width - _ETA_WIDTH - 2*_COL_GAP
            for i in range(n_textlines):
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
                    #print("i:", i, "dest_sz: ", dest_sz, "dest_width:", dest_width)
                    show = True
                    if states[_S_BLINKING] and blink_hide:
                        disp.rectangle(dest_off, row_y, dest_width, 8)
                        show = False
                    if dest_sz > dest_width:
                        dest_sz += min(separator_sz, _TEXT_BUF_SZ - _ETA_WIDTH - line_width - dest_sz)
                        x = states[_S_DEST_X]
                        next_x = x+1
                        if next_x == dest_sz:
                            next_x = _DEST_SCROLL_INIT
                        states[_S_DEST_X] = next_x
                        if x < 0:
                            x = 0
                        #print("x: ", x)
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
            if _warn_msg_sz:
                warn_buf_lock.acquire()
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
            elif warn_msg_sz:
                disp.rectangle(0, warn_y, disp_width, 8)
                warn_msg_sz = 0
            if t1 > t_blink:
                t_blink += _BLINK_DELAY_MS
                blink_hide = not blink_hide
            # elapsed = time.ticks_diff(time.ticks_ms(), t1)
            # print("elapsed", elapsed)
            # busy loop on core 2 to avoid flickering
            t1 += _FRAME_DELAY
            while True:
                if time.ticks_ms() >= t1:
                    break
            h75.update(disp)
    except Exception as e:
        import sys
        print(f"display thread failed: {e}")
        sys.print_exception(e)
    finally:
        print("display thread finished")
        _disp_thread_lock.release()

_dep_data = None
_safe_to_fetch = asyncio.Event()
_safe_to_fetch.set()

async def render_task():
    global _dest_offset, _textlines
    print("render task started")
    await asyncio.sleep(5) # give a chance to read the IP address
    start_ms = 0
    colored = False
    sub_colors = False
    print("waiting for first data")
    console("waiting for data...")
    while _dep_data == None:
        await asyncio.sleep_ms(100)
    _disp.set_pen(0)
    _disp.clear()
    gc.collect()
    # _textlines[row] structure: ETA|LINE|DEST
    zero_states = ["", "", "", 0, 0, False]
    _textlines = []
    for _ in range(_n_textlines):
        buf, mv = make_col(_TEXT_BUF_SZ)
        _textlines.append((
            buf, mv,
            (_thread.allocate_lock(), _thread.allocate_lock(),_thread.allocate_lock()),
            zero_states[:]
        ))

    _thread.start_new_thread(display_thread, ())
    _safe_to_fetch.clear()
    t1 = time.ticks_ms()
    line_size_max = _LINE_MIN_WIDTH
    while True:
        try:
            data = _dep_data
            now = time.time()
            force_update = False
            def update(old, new):
                nonlocal force_update
                if old != new:
                    force_update = True
                return new
            walk_delay = settings.get("WALK_DELAY")
            colored = update(colored, settings.get("COLORED"))
            sub_colors = update(sub_colors, settings.get("SUBWAY_COLORS"))
            if settings.check():
                line_size_max = _LINE_MIN_WIDTH
            dest_offset = line_size_max + _COL_GAP
            if _dest_offset != dest_offset:
                _dest_offset = dest_offset
                _disp.clear()
                force_update = True
            line_width = line_size_max
            i = 0
            reset_dest_x = []
            for line, typ, dest, when, bg_col in data:
                if i >= _n_textlines:
                    break

                eta_n = when - now + 45
                if eta_n < walk_delay:
                    continue

                eta_n //= 60

                blinking = False
                if eta_n < 1:
                    eta_s = ""
                    blinking = True
                else:
                    eta_s = str(eta_n) + "'"

                line_size = render_text(line, bold=True, kerning=True)
                line_size_max = max(line_size_max, line_size)
                line_offset = max(line_width - line_size, 0)
                eta_size = 0
                if eta_s:
                    eta_size = render_text(eta_s)

                tl_buf, _, locks, states = _textlines[i]

                # render ETA at beginning of textline buffer
                if eta_s != states[_S_ETA] or force_update:
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
                if line != states[_S_LINE] or force_update:
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
                if dest != states[_S_DEST] or force_update:
                    with locks[_S_DEST]:
                        tl_buf.set_pen(_BG)
                        tl_buf.rectangle(_ETA_WIDTH + line_width, 0, _TEXT_BUF_SZ - _ETA_WIDTH - line_width, 8)
                        set_pen(tl_buf, _BVG)
                        states[_S_DEST_SZ] = render_text(dest + _separator, tl_buf, _ETA_WIDTH + line_width, 0, clip=_TEXT_BUF_SZ - _ETA_WIDTH - line_width, kerning=True) - _separator_sz
                        states[_S_DEST] = dest
                        reset_dest_x.append(i)
                        print(f"updated DEST of line {i}")
                i += 1

            for j in reset_dest_x:
                locks = _textlines[j][2]
                locks[_S_DEST].acquire()
            for j in reset_dest_x:
                states = _textlines[j][3]
                states[_S_DEST_X] = _DEST_SCROLL_INIT
            for j in reset_dest_x:
                locks = _textlines[j][2]
                locks[_S_DEST].release()

            # Clear any unused rows
            for j in range(i, _n_textlines):
                disp, _, locks, states = _textlines[j]
                for lock in locks:
                    lock.acquire()
                disp.set_pen(_BG)
                disp.clear()
                states[:] = zero_states[:]
                for lock in locks:
                    lock.release()

        except Exception as e:
            import sys
            print(f"render task failed: {e}")
            sys.print_exception(e)
            print("last data:", data)
        # print("---")

        # Signal fetch task: "I'm done, safe to run now"
        _safe_to_fetch.set()  # Wake up fetch task
        await asyncio.sleep_ms(0) # Yield to let fetch see signal
        _safe_to_fetch.clear()  # Clear immediately (edge-trigger)
        t1 += 1000
        delay = time.ticks_diff(t1, time.ticks_ms())
        #print("render delay:", delay)
        if delay > 0:
            await asyncio.sleep_ms(delay)
        else:
            print("render_task took too long:", delay)
            t1 -= delay

_time_set = False

@micropython.native
def parse_color(s):
    if not s:
        return None
    s = s[1:]  # strip '#'
    if len(s) == 3:
        r, g, b = (int(c, 16) * 0x11 for c in s)
    else:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return (r, g, b)

async def data_fetch_task():
    """Fetches data every 10 seconds"""
    global _dep_data, _time_set
    print("fetch task started")
    current_warn_id = 0
    while True:
        try:
            await _safe_to_fetch.wait()
            gc.collect()
            # print("fetching data")
            params = {
                "results": "14",
                "duration": "60",
                "pretty": "true",
                "sparse": "true",
                "language": "de",
            }
            if not settings.get("SHOW_BUS"): params["bus"] = "false"
            if not settings.get("SHOW_TRAM"): params["tram"] = "false"
            if not settings.get("SHOW_SUBWAY"): params["subway"] = "false"
            if not settings.get("SHOW_REGIONAL"): params["regional"] = "false"
            if not settings.get("SHOW_SUBURBAN"): params["suburban"] = "false"
            if not settings.get("SHOW_FERRY"): params["ferry"] = "false"
            if not settings.get("SHOW_EXPRESS"): params["express"] = "false"
            if _time_set: params["when"] = str(time.time() + int(settings.get("WALK_DELAY")))
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f'{settings.get("API_URL")}/stops/{settings.get("STATION_ID")}/departures',
                    params=params,
                ) as response:
                    # print("got response")
                    if response.status == 200:
                        date = parse_http_date(response.headers["Date"])
                        old_t = time.time()
                        _rtc.datetime(date)
                        if _time_set:
                            diff = time.time() - old_t
                            if diff < -1 or diff > 1:
                                print("time diff:", diff)

                        if not _time_set:
                            _time_set = True
                            print("time set")
                            continue

                        parse_start_ms = time.ticks_ms()
                        # Stream parse JSON response
                        data = await response.json()
                        print(
                            "parsing:", time.ticks_diff(time.ticks_ms(), parse_start_ms)
                        )

                        # Process parsed departures
                        new_data = []
                        filtered = settings.get("FILTERED")
                        for dep in data["departures"]:
                            when = dep["when"]
                            if not when:
                                continue
                            when = parse_iso_to_epoch(when)
                            line_obj = dep["line"]
                            line = line_obj["name"]
                            if line in filtered:
                                continue
                            typ = line_obj["product"]
                            # Clean up destination
                            dest = dep["direction"] #.split(", ")[-1]
                            dest = dest.replace(" [Endstelle]", "")
                            dest = dest.replace(" (Berlin)", "")
                            dest = dest.replace("  ", " ")
                            dest = dest.strip()
                            bg = parse_color(line_obj.get("color", {}).get("bg"))

                            new_data.append((line, typ, dest, when, bg))
                        # new_data[1] = (new_data[1][0], new_data[1][1], "S+U Zoologischer Garten", new_data[1][3], new_data[1][4])
                        _dep_data = new_data
                        warn_id = 0
                        warn_msg = ""
                        prio_min = 100
                        if (time.time() % _WARN_CYCLE) > _WARN_PAUSE:
                            for warn in data["warnings"]:
                                #print("warn:", warn)
                                now = time.time()
                                prio = warn["priority"]
                                if (prio >= prio_min
                                        and now >= parse_iso_to_epoch(warn["validFrom"])
                                        and now <= parse_iso_to_epoch(warn["validUntil"])):
                                    warn_id = warn["id"]
                                    summary = warn["summary"]
                                    text = warn["text"].split("\n")[0]
                                    text = text.replace(" [Endstelle]", "")
                                    text = text.replace(" (Berlin)", "")
                                    text = text.replace("  ", " ")
                                    warn_msg = f'{summary}: {text} *** '
                                    prio_min = prio+1
                            # if not warn_id:
                            #     warn_id = 99
                            #     warn_msg = "Unterbrechung: Tram M1: Die Linie fährt aufgrund von Bauarbeiten nicht zwischen S Hackescher Markt und S+U Friedrichstraße. Umfahrung: tagsüber M5, S3, S5, S7, S9 & nachts Ersatzverkehr M1 bis S+U Friedrichstraße (Am Kupfergraben). *** "
                        if warn_id != current_warn_id:
                            current_warn_id = warn_id
                            warn_msg_update(warn_msg)
                        print("updated data")
                    else:
                        print("fetch failed:", response.status)

        except Exception as e:
            import sys

            print(f"Fetch task failed: {e}")
            sys.print_exception(e)

        print("memfree:", gc.mem_free())
        # print("fetch finished")
        await asyncio.sleep(_FETCH_DELAY)


        # print("waiting for finished display")


async def check_night_time_task():
    """Check if current time is within night hours"""
    global _dimming, _time_set
    while not _time_set:
        await asyncio.sleep(1)
    while True:
        print("check_night_time")
        now = _rtc.datetime()
        # RTC is UTC, apply timezone offset
        tz_offset = settings.get("TIMEZONE") or 0
        current_minutes = (now[4] + tz_offset) * 60 + now[5]  # (hours + tz) * 60 + minutes
        # Normalize to 0-1439 range (24 hours = 1440 minutes)
        current_minutes = current_minutes % 1440 + 1

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
            warn_msg_update()

        await asyncio.sleep(85 - (now[6] + 30) % 60)

# Main entry point
async def main():
    from web_server import start_web_server

    # Check if WiFi credentials are configured
    ssid = settings.get("WIFI_SSID")
    password = settings.get("WIFI_PASSWORD")
    ap_mode = False

    if not ssid or ssid == "REPLACE_WITH_YOUR_SSID":
        console("No WiFi configured", clear=True)
        start_ap_mode()
        ap_mode = True
    elif not network_connect(ssid, password):
        console("Starting AP mode...")
        start_ap_mode()
        ap_mode = True
    else:
        connectivity_test()
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

_warn_buf  = make_col(_WARN_BUF_SZ)
_warn_buf_lock = _thread.allocate_lock()
_warn_msg = ""
_warn_msg_sz = 0

# Start the event loop
try:
    asyncio.run(main())
finally:
    print("shutting down")
    _h75.stop()
    _disp_thread_stop = True
    _disp_thread_lock.acquire()
    _disp_thread_lock.release()
