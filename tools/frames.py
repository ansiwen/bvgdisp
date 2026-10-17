# Runs bvgdisp.py's render task and display thread in the MicroPython unix
# port, with stubbed hardware, and records every frame:
#
#   micropython -X heapsize=32M tools/frames.py SRC_DIR DIMMING OUT [--mem]
#                                               [--long] [--mutate]
#
# SRC_DIR is a src/ tree, eg. src or one extracted from an older revision.
# It writes the SHA-256 of each frame buffer to OUT; compare_frames.sh
# compares two revisions this way. The departures, the warning and the
# clock are fixed, so the frames are deterministic: 3000 frames (100 s),
# with scrolling, a blinking row, the ticker and a change of the data.
#
# --mem   instead, prints the bytes the display thread allocates per frame
#         (must be 0)
# --long  a destination and a warning longer than the text buffers, to
#         check clipping; prints their widths
# --mutate  draws ETA and DEST in another pen, to see that a comparison
#         catches a difference
#
# blit_asm() can't run here: it's replaced by a viper stand-in that reads
# the same parameter block. Every blit() call is checked against the bounds
# that its docstring requires.
import sys, builtins, struct, hashlib, asyncio, gc, _thread, time as _time

SRC = sys.argv[1]
DIM = int(sys.argv[2])
OUT = sys.argv[3]
MEM = "--mem" in sys.argv
LONG = "--long" in sys.argv
MUTATE = "--mutate" in sys.argv
sys.path.insert(0, SRC)


class PicoGraphics:  # RGB888, as on the device
    def __init__(self, width=None, height=None, display=None, pen_type=None):
        if display == 1:
            width, height = 128, 64
        self.w, self.h = width, height
        self.buf = bytearray(width * height * 4)
        self.zero = bytes(len(self.buf))
        self.pen = 0
    def set_pen(self, p): self.pen = p
    def create_pen(self, r, g, b): return r << 16 | g << 8 | b
    def get_bounds(self): return (self.w, self.h)
    def pixel(self, x, y):
        if 0 <= x < self.w and 0 <= y < self.h:
            struct.pack_into("<I", self.buf, (y * self.w + x) * 4, self.pen)
    def rectangle(self, x, y, w, h):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.pixel(xx, yy)
    def clear(self):
        if self.pen == 0:
            self.buf[:] = self.zero
        else:
            self.rectangle(0, 0, self.w, self.h)
    def text(self, *a, **k): pass

class picographics:
    PicoGraphics = PicoGraphics
    DISPLAY_INTERSTATE75_128X64 = 1
    DISPLAY_GENERIC = 2
    PEN_RGB888 = 3

DIGESTS = []
MEMS = [0] * 4000
REC = [False, 0, 0]  # recording, frames to record, frames recorded
class Hub75:
    def __init__(self, w, h, color_order=None): pass
    def start(self): pass
    def stop(self): pass
    def clear(self): pass
    def set_pixel(self, *a): pass
    def update(self, disp, *a):
        if REC[0]:
            if MEM:
                MEMS[REC[2]] = gc.mem_alloc()
            else:
                DIGESTS.append(hashlib.sha256(disp.buf).digest())
            REC[2] += 1
            if REC[2] >= REC[1]:
                g["_disp_thread_stop"] = True
class hub75:
    Hub75 = Hub75
    COLOR_ORDER_RGB = 0
class hw_conf:
    DISPLAY = 1
    COLOR_ORDER = 0
class machine:
    class RTC:
        def datetime(self, *a): return (2026, 10, 16, 0, 12, 0, 0, 0)
    def reset(): raise SystemExit
class WLAN:
    def __init__(self, *a): pass
class network:
    STA_IF, AP_IF = 0, 1
    WLAN = WLAN
    def country(c): pass
    def hostname(h): pass
for name, mod in (("picographics", picographics), ("hub75", hub75), ("hw_conf", hw_conf),
                  ("machine", machine), ("network", network)):
    sys.modules[name] = mod

STAND_IN = '''@micropython.viper
def blit_asm(p: ptr32, pen: uint):
    # stand-in with the contract of the asm: reads the same parameter
    # block. The block has 32-bit addresses; on 64-bit, take the upper half
    # from the block's own address (all buffers are in the same heap).
    hi = uint(p) - (uint(p) & uint(0xFFFFFFFF))
    shift = int(p[3])
    length = int(p[5])
    lines = int(p[2])
    d = hi | uint(p[0])
    s = hi | uint(p[1])
    while lines > 0:
        i = 0
        while i < length:
            x = shift + i
            if (uint(ptr32(s)[x >> 5]) >> uint(x & 31)) & 1:
                ptr32(d)[i] = pen
            i += 1
        d += uint(p[6])
        s += uint(p[7])
        lines -= 1

'''
src = open(SRC + "/bvgdisp.py").read()
src = src[:src.index("# Start the event loop")].replace("\nbanner()\n", "\n")
asm = src.find("@micropython.asm_thumb\ndef blit_asm(")
if asm >= 0:
    src = src[:asm] + STAND_IN + src[src.index("@micropython.viper\ndef blit(", asm):]
g = {"__name__": "bvgdisp",
     "memoryview": lambda o: builtins.memoryview(o.buf if isinstance(o, PicoGraphics) else o)}
exec(src, g)

BLITS = [0]
_blit = g["blit"]
def checked_blit(*a):
    if len(a) == 8:  # blit() since the 1-bit buffers; older ones aren't checked
        dest, src, pen, dst_off, dst_width, src_off, src_width, length = a
        assert length >= 0 and src_off >= 0 and src_off + length <= src_width, (src_off, length, src_width)
        assert len(src) == src_width + 4, (len(src), src_width)
        assert dst_off >= 0 and dst_off % dst_width + length <= dst_width, (dst_off, length)
        assert dst_off + 7 * dst_width + length <= 128 * 64, dst_off
        BLITS[0] += 1
    return _blit(*a)
if not MEM:  # the checks allocate themselves
    g["blit"] = checked_blit

NOW = 1790000000
class fake_time:
    T = [0]
    def ticks_ms():
        fake_time.T[0] += 1
        return fake_time.T[0]
    U = [0]
    def ticks_us():  # for _FRAME_STATS
        fake_time.U[0] += 37
        return fake_time.U[0]
    ticks_add = _time.ticks_add
    ticks_diff = _time.ticks_diff
    def time(): return NOW  # an int, as on rp2 (the unix port returns a float)
    def sleep(s): pass
    def sleep_ms(ms): pass
class fake_asyncio:
    Event = asyncio.Event
    def sleep(s): return asyncio.sleep_ms(0)
    def sleep_ms(ms): return asyncio.sleep_ms(0)
class fake_thread:
    allocate_lock = _thread.allocate_lock
    def start_new_thread(f, a): pass
g["time"] = fake_time
g["asyncio"] = fake_asyncio
g["_thread"] = fake_thread

g["_dimming"] = DIM
if "make_pen" in g:  # check_night_time_task sets both
    g["_pen_bvg"] = g["make_pen"]((40, 255, 40) if MUTATE else (255, 170, 0))

DATA_A = [("U5", "subway", "Hönow", NOW + 10, (126, 83, 48)),
          ("M10", "tram", "S+U Warschauer Str. via Hauptbahnhof", NOW + 300, None),
          ("S41", "suburban", "Ringbahn S 41 ⟳", NOW + 600, None),
          ("200", "bus", "Michelangelostr.", NOW + 900, None),
          ("RE1", "regional", "Frankfurt (Oder) über Erkner, Fürstenwalde", NOW + 1200, None),
          ("X9", "bus", "Flughafen BER Terminal 1-2", NOW + 1500, None),
          ("U2", "subway", "Ruhleben", NOW + 1800, (218, 66, 30)),
          ("N5", "bus", "Hönow", NOW + 2400, None)]
DATA_B = [("FEX", "regional", "Flughafen BER", NOW + 70, None),
          ("U5", "subway", "Hauptbahnhof", NOW + 10, (126, 83, 48)),
          ("M10", "tram", "Clara-Jaschke-Str./Hauptbahnhof Nord §2", NOW + 400, None),
          ("S41", "suburban", "Ringbahn S 41 ⟲", NOW + 650, None)]
WARN = "Bauarbeiten: Zwischen Alexanderplatz und Hönow fährt die U5 nur alle 20 Minuten. Bitte nutzen Sie die Busse. *** "
LONG_DEST = "S+U Hauptbahnhof via Perleberger Brücke, Lehrter Str., Invalidenpark, Naturkundemuseum, Nordbahnhof, Bernauer Str."
if LONG:
    DATA_B = [("TXL", "bus", LONG_DEST, NOW + 200, None)] + DATA_B

async def render(cycles):
    t = asyncio.create_task(g["render_task"]())
    for _ in range(cycles):
        await asyncio.sleep_ms(0)
    t.cancel()

def display(n):
    if MEM:
        gc.collect()
        gc.disable()
    REC[0], REC[1] = True, REC[2] + n
    g["_disp_thread_stop"] = False
    g["display_thread"]()
    REC[0] = False
    gc.enable()

g["_dep_data"] = DATA_A
asyncio.run(render(40))
g["set_warn_msg"](WARN)
display(1500)
g["_dep_data"] = DATA_B
asyncio.run(render(40))
g["set_warn_msg"](WARN * 9 if LONG else WARN[:40])
if LONG:
    rt = g["render_text"]
    print("warning: %d px, in the ticker: %d px" % (rt(WARN * 9, kerning=True), g["_warn_msg_sz"]))
    row = [r for r in g["_textlines"] if r[2][2] == LONG_DEST][0]
    print("long destination: %d px, DEST_SZ %d (DEST starts at x = %d)"
          % (rt(LONG_DEST, kerning=True), row[2][3], 11 + g["_dest_offset"] - 3))
display(1500)

name = SRC.rstrip("/") + " dimming %d" % DIM
if MEM:
    d = [MEMS[i + 1] - MEMS[i] for i in range(REC[2] - 1) if i != 1499]
    print(name, ": display thread allocated max %d B per frame, %d B in total, over %d frames"
          % (max(d), sum(d), len(d)))
else:
    with open(OUT, "wb") as f:
        for d in DIGESTS:
            f.write(d)
    print(name, ": %d frames (%d distinct), %d blit() calls checked"
          % (len(DIGESTS), len(set(DIGESTS)), BLITS[0]))
