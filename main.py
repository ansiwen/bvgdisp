import time
import socket
import machine
import network
import asyncio
import aiohttp
from hub75 import Hub75
from picographics import PicoGraphics
from font_bvg import font_small
import settings
import hw_conf

parser_departures = []
parser_partial_dep = b''

def parser_clear():
    global parser_departures, parser_partial_dep
    parser_departures = []
    parser_partial_dep = b''

def parser_feed(chunk):
    global parser_departures, parser_partial_dep
    #print("feed:", len(chunk))

    data = parser_partial_dep + chunk if parser_partial_dep else chunk

    offset = len(parser_partial_dep)-3
    if offset < 0:
        offset = 0

    end = 0

    while True:
        if end > 0:
            data = data[end:]
        end = data.find(b'\n\t\t}', offset)
        if end == -1:
            parser_partial_dep = data
            return
        end += 4

        offset = 0

        pos = data.find(b'\n\t\t\t"when": "', 0)
        if pos == -1:
            continue
        pos += 13
        nl = data.find(b'\n', pos)
        when = data[pos:nl-2].decode()

        pos = data.find(b'\n\t\t\t"direction"', nl)
        if pos == -1:
            continue
        pos += 18
        nl = data.find(b'\n', pos)
        direction = data[pos:nl-2].decode()

        pos = data.find(b'\n\t\t\t"line"', nl)
        if pos == -1:
            continue
        pos += 13
        pos = data.find(b'\n\t\t\t\t"name"', pos)
        if pos == -1:
            continue
        pos += 14
        nl = data.find(b'\n', pos)
        line = data[pos:nl-2].decode()

        pos = data.find(b'\n\t\t\t\t"product"', nl)
        if pos == -1:
            continue
        pos += 17
        nl = data.find(b'\n', pos)
        product = data[pos:nl-2].decode()

        #print("dep:", line, product, direction, when)
        parser_departures.append((line, product, direction, when))


rtc = machine.RTC()

# Enable the Wireless
network.country("DE")
network.hostname("BVGdisplay")

wlan = network.WLAN(network.STA_IF)
ap_mode = False

# Setup for the display
display = PicoGraphics(display=hw_conf.DISPLAY)

WIDTH, HEIGHT = display.get_bounds()

h75 = Hub75(WIDTH, HEIGHT, color_order=hw_conf.COLOR_ORDER)
h75.start()


# Colors as tuples (R, G, B)
RED = (120, 0, 0)
YELLOW = (255, 180, 0)
BVG = (255, 170, 0)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

is_night_time = False

def set_pen(color):
    """Set pen with dimming applied. Color is (R, G, B) tuple."""
    dim = settings.get('NIGHT_DIMMING')
    if dim == 10 or not is_night_time:
        display.set_pen(display.create_pen(*color))
    else:
        dimmed = tuple(v * dim // 10 for v in color)
        display.set_pen(display.create_pen(*dimmed))

console_y = 0
def console(*args, clear=False):
    global console_y
    set_pen(RED)
    if clear:
        console_y=0
        set_pen(BLACK)
        display.clear()
        display
        set_pen(RED)
    str_args = [str(arg) for arg in args]
    s = ' '.join(str_args)
    print(s)
    display.text(s, 0, console_y-1, scale=1)
    h75.update(display)
    console_y+=6

def start_ap_mode():
    """Start WiFi Access Point for configuration"""
    global ap_mode
    ap_mode = True
    ap = network.WLAN(network.AP_IF)


    # Disable station mode
    wlan.active(False)

    # Enable AP mode
    ap.config(essid='BVGdisplay', security=0)  # Open network for easy setup
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
        wlan.disconnect()
        while wlan.active():
            wlan.active(False)
        while not wlan.active():
            wlan.active(True)
        # Sets the Wireless LED pulsing and attempts to connect to your local network.
        console(f"connecting to {SSID}...", clear=True)
        wlan.config(pm=0xa11140)  # Turn WiFi power saving off for some slow APs
        wlan.connect(SSID, PSK)

        for j in range(10):
            print("wlan.status:", wlan.status())
            if wlan.status() < 0 or wlan.status() >= 3:
                break
            print('waiting for connection...')
            time.sleep(1)

        # Handle connection error. Switches the Warn LED on.
        if wlan.isconnected():
            print("connected")
            ip = wlan.ifconfig()[0]
            console("IP:", ip)
            return True

        print("wlan.status:", wlan.status())
        console("Unable to connect.")
        console("Retrying...")
        time.sleep(2)
    console("Failed to connect.")
    return False

def connectivity_test(host='1.1.1.1', port=80, timeout=60):
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
    tz_sign = 1 if tz_part[0] == '+' else -1
    tz_hours = int(tz_part[1:3])
    tz_mins = int(tz_part[4:6])
    tz_offset = tz_sign * (tz_hours * 3600 + tz_mins * 60)
    
    # Convert to epoch (UTC)
    # mktime expects: (year, month, day, hour, min, sec, weekday, yearday)
    timestamp = time.mktime((year, month, day, hour, minute, second, 0, 0))
    
    # Adjust for timezone (subtract to convert to UTC)
    return timestamp - tz_offset

def parse_http_date(date_str):
    # "Mon, 05 Jan 2026 19:17:30 GMT"
    import time
    
    months = {
        'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4,
        'May': 5, 'Jun': 6, 'Jul': 7, 'Aug': 8,
        'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
    }

    parts = date_str.split()
    day = int(parts[1])
    month = months[parts[2]]
    year = int(parts[3])

    hour, minute, second = map(int, parts[4].split(':'))

    # RTC.datetime() format: (year, month, day, weekday, hours, minutes, seconds, subseconds)
    timestamp = (year, month, day, 0, hour, minute, second, 0)

    return timestamp

# def text_bold(txt, x, y, scale=1):
#     for c in txt:
#         print("printing bold ", c, " at ", x, y)
#         display.text(c, x, y, scale=scale)
#         #display.text(c, x+1, y, scale=scale)
#         x += display.measure_text(c, scale)+2
        
# def scroll_x():
#     fb = memoryview(display)
#     for y in range(32):
#         c = 4*WIDTH
#         x0 = bytes(fb[c*y:c*y+3])
#         fb[c*y:c*(y+1)-4] = fb[c*y+4:c*(y+1)]
#         fb[c*(y+1)-4:c*(y+1)-1] = x0

def banner():
    import pngdec
    png = pngdec.PNG(display)
    png.open_file("ansiwen128x64.png")
    pos_y = HEIGHT//2 - 16
    for y in range(33):
        png.decode(0, pos_y, source=(0, y, 128, 32))
        h75.update(display)
    time.sleep_ms(200)
    set_pen(BLACK)
    for i in range(10):
        time.sleep_ms(20)
        display.clear()
        h75.update(display)
        png.decode(0, pos_y, source=(0, 32, 128, 32))
        h75.update(display)
    for i in range(16):
        time.sleep_ms(10)
        display.line(0, pos_y+i, 128, pos_y+i)
        display.line(0, pos_y+31-i, 128, pos_y+31-i)
        h75.update(display)
    set_pen(WHITE)
    pos_y += 16
    display.line(0, pos_y, 128, pos_y)
    set_pen(BLACK)
    for i in range(64):
        time.sleep_ms(3)
        display.pixel(i, pos_y)
        display.pixel(127-i, pos_y)
        h75.update(display)

def normalize(a, b, c):
    max = a
    if b > max:
        max = b
    if c > max:
        max = c
    a = a*255//max
    b = b*255//max
    c = c*255//max
    return a, b, c

def typ2col(t, l):
    """Return color tuple for transport type and line"""
    if t == "tram" or t == "regional":
        return (190, 20, 20)
    elif t == "bus":
        return (149, 39, 110)
    elif t == "suburban":
        return (0, 141, 79)
    elif t == "subway":
        if settings.get('SUBWAY_COLORS'):
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
        return (17, 93, 145)
    return BVG

def pprint(s, x=0, y=0, bold=False, clip=WIDTH, skip=0, measure=False, kerning=False):
    if not s:
        return 0
    cursor_x = x-skip
    height = font_small["fontheight"]
    last_col = [False]*(height+2)
    for char in s:
        if cursor_x >= clip:
            # invisible
            break
        if bold:
            glyph = font_small.get("*"+char)
        if not bold or glyph == None:
            glyph = font_small.get(char)
        if glyph is None:
            continue
        width = glyph[0]-1
        if kerning:
            for row in range(height):
                if bold:
                    check = glyph[row+1]>>width and (last_col[row+1])
                else:
                    check = glyph[row+1]>>width and (last_col[row] or last_col[row+1] or last_col[row+2])
                if check:
                    cursor_x += 1
                    break
            last_col = [False]*(height+2)
        if cursor_x + width > clip:
            # don't print partial character
            break
        if cursor_x + width <= x:
            # invisible
            cursor_x += width
            continue
        for row in range(height):
            byte_val = glyph[row+1]
            if kerning:
                last_col[row+1] = byte_val&2 == 2
            if measure:
                continue
            for col in range(width):
                px = cursor_x + col
                if px >= clip:
                    # invisible
                    break
                if px < x:
                    # invisible
                    continue
                if byte_val & (0x1 << (width-col)):
                    display.pixel(px, y + row)
        # Move cursor for next character
        cursor_x += width
        if not kerning:
            cursor_x += 1
    return cursor_x - x    

banner()

set_pen(RED)

# Check if WiFi credentials are configured
ssid = settings.get('WIFI_SSID')
password = settings.get('WIFI_PASSWORD')

if not ssid or ssid == "REPLACE_WITH_YOUR_SSID":
    console("No WiFi configured", clear=True)
    start_ap_mode()
elif not network_connect(ssid, password):
    console("Starting AP mode...")
    start_ap_mode()
else:
    connectivity_test()
    console("connected to internet")
    console("waiting for data...")

shared_data = []
safe_to_fetch = asyncio.Event()
safe_to_fetch.set()

display.set_font("bitmap8")

async def display_task():
    print("display task started")
    await asyncio.sleep_ms(500)
    start_ms = 0
    blink = True
    print("waiting for first data")
    while not shared_data:
        await asyncio.sleep_ms(100)
    safe_to_fetch.clear()
    while True:
        try:
            last_ms = start_ms
            start_ms = time.ticks_ms()
            delta = time.ticks_diff(start_ms, last_ms)
            if last_ms > 0 and delta > 1050:
                print("delta:", time.ticks_diff(start_ms, last_ms))
        
            data = shared_data

            set_pen(BLACK)
            display.clear()

            now = time.time()

            set_pen(BVG)

            y = 0
            if HEIGHT == 64:
                y = 1
            
            for (line, typ, dest, when) in data:
                if y > HEIGHT-8:
                    break

                #print("when", when, "now", now)
                eta_n = (when-now+45)
                if eta_n < settings.get('WALK_DELAY'):
                    continue

                eta_n //= 60

                #print(line, dest, eta_n)
                if eta_n < 1:
                    eta_s = None
                    #eta_s = str(eta_n) + "'"
                    if blink:
                        dest = None
                else:
                    eta_s = str(eta_n) + "'"
                if settings.get('COLORED'):
                    set_pen(typ2col(typ, line))
#                display.rectangle(0, y, dest_offset-2, 8)
#                display.set_pen(BLACK)
                dest_offset = settings.get('DEST_OFFSET')
                line_size = pprint(line, 0, y, bold=True, kerning=True, measure=True)
                pprint(line, dest_offset-line_size-3, y, bold=True, kerning=True)
                set_pen(BVG)
                dest_width = WIDTH - dest_offset - pprint("30'", measure=True) - 1
                pprint(dest, dest_offset, y, clip=dest_offset+dest_width, kerning=True)
                eta_offset = WIDTH - pprint(eta_s, measure=True)+1
                pprint(eta_s, eta_offset, y)
                y += 8
                if HEIGHT == 64:
                    y += 1
                
            h75.update(display)
        except Exception as e:
            import sys
            print(f"Display task failed: {e}")
            sys.print_exception(e)
            print("last data:", data)
        #print("---")
        blink = not blink

        # Signal fetch task: "I'm done, safe to run now"
        safe_to_fetch.set()           # Wake up fetch task
        await asyncio.sleep_ms(0)       # Yield to let fetch see signal
        safe_to_fetch.clear()         # Clear immediately (edge-trigger)

        elapsed_ms = time.ticks_diff(time.ticks_ms(), start_ms)
        await asyncio.sleep_ms(max(1, 1000 - elapsed_ms))

async def data_fetch_task():
    """Fetches data every 10 seconds"""
    global shared_data, parser_departures
    print("fetch task started")
    time_set = False
    await asyncio.sleep(5) # give chance to read IP address
    while True:
        try:
            await safe_to_fetch.wait()
#            print("fetching data")
            params = {
                "results": "14",
                "duration": "30",
                "pretty": "true",
                "bus": "true" if settings.get('SHOW_BUS') else "false",
                "tram": "true" if settings.get('SHOW_TRAM') else "false",
                "subway": "true" if settings.get('SHOW_SUBWAY') else "false",
                "regional": "true" if settings.get('SHOW_REGIONAL') else "false",
                "suburban": "true" if settings.get('SHOW_SUBURBAN') else "false",
                "ferry": "true" if settings.get('SHOW_FERRY') else "false",
                "express": "true" if settings.get('SHOW_EXPRESS') else "false"
            }
            if time_set:
                params["when"] = time.time()+settings.get('WALK_DELAY')
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f'{settings.get("API_URL")}/stops/{settings.get("STATION_ID")}/departures',
                        params=params
                    ) as response:
                        #print("got response")
                        if response.status == 200:
                            date = parse_http_date(response.headers["Date"])
                            old_t = time.time()
                            rtc.datetime(date)
                            if time_set:
                                diff = time.time() - old_t
                                if diff < -1 or diff > 1:
                                    print("time diff:", diff)
                            
                            if not time_set:
                                time_set = True
                                print("time set")
                                continue

                            # Stream parse JSON response
                            while True:
                                chunk = await response.content.read(1024)
                                if not chunk:
                                    break
                                parser_feed(chunk)

                            # Process parsed departures
                            new_data = []
                            filtered = settings.get('FILTERED')
                            for (line, typ, dest, when_str) in parser_departures:
                                if line in filtered:
                                    continue
                                when = parse_iso_to_epoch(when_str)
                                # Clean up destination
                                dest = dest.split(", ")[-1]
                                dest = dest.replace("[Endstelle]", "")
                                dest = dest.replace("(Berlin)", "")
                                dest = dest.replace("  ", " ")

                                new_data.append((line, typ, dest, when))

                            parser_clear()

                            if new_data:
                                shared_data = new_data
                                print("updated data")
                        else:
                            print("fetch failed:", response.status)
                            
        except Exception as e:
            import sys
            print(f"Fetch task failed: {e}")
            sys.print_exception(e)

#        print("fetch finished")
        await asyncio.sleep(10)
#        print("waiting for finished display")

async def check_night_time_task():
    """Check if current time is within night hours"""
    global is_night_time
    while True:
        print("check_night_time")
        now = rtc.datetime()
        # RTC is UTC, apply timezone offset
        tz_offset = settings.get('TIMEZONE') or 0
        current_minutes = (now[4] + tz_offset) * 60 + now[5]  # (hours + tz) * 60 + minutes
        # Normalize to 0-1439 range (24 hours = 1440 minutes)
        current_minutes = current_minutes % 1440

        start = settings.get('NIGHT_START')
        end = settings.get('NIGHT_END')

        start_h, start_m = map(int, start.split(':'))
        end_h, end_m = map(int, end.split(':'))

        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        if start_minutes <= end_minutes:
            # Same day range (e.g., 08:00 to 18:00)
            is_night_time = start_minutes <= current_minutes < end_minutes
        else:
            # Overnight range (e.g., 22:00 to 06:00)
            is_night_time = current_minutes >= start_minutes or current_minutes < end_minutes
        await asyncio.sleep(60-now[6]%60)


# Main entry point
async def main():
    from web_server import start_web_server

    # Start web server for settings configuration
    web_server = await start_web_server(port=80)

    if ap_mode:
        # In AP mode, only run the web server for configuration
        await asyncio.Event().wait() # wait forever
    else:
        # Normal mode: run data fetcher and display
        fetcher = asyncio.create_task(data_fetch_task())
        display_t = asyncio.create_task(display_task())
        check_night_time = asyncio.create_task(check_night_time_task())
        await asyncio.gather(fetcher, display_t, check_night_time)

    web_server.close()

# Start the event loop
asyncio.run(main())

machine.reset()