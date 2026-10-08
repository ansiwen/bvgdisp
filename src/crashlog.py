import sys
import os
import io
import gc
import time

CRASH_LOG = "/crash.log"
_MAX_SIZE = 8192  # rotate to CRASH_LOG + ".1" beyond this, to spare flash


def format_exception(where, e):
    """Return a timestamped backtrace of e as a string (doesn't touch flash)."""
    gc.collect()
    t = time.gmtime()
    buf = io.StringIO()
    buf.write("=== %04d-%02d-%02d %02d:%02d:%02d UTC, uptime %ds, mem_free %d, in %s\n" % (
        t[0], t[1], t[2], t[3], t[4], t[5], time.ticks_ms() // 1000, gc.mem_free(), where))
    sys.print_exception(e, buf)
    return buf.getvalue()


def write(text):
    """Append text to the crash log. Call this only from core 0."""
    try:
        try:
            if os.stat(CRASH_LOG)[6] > _MAX_SIZE:
                try:
                    os.remove(CRASH_LOG + ".1")
                except OSError:
                    pass
                os.rename(CRASH_LOG, CRASH_LOG + ".1")
        except OSError:
            pass
        with open(CRASH_LOG, "a") as f:
            f.write(text)
        print("backtrace written to", CRASH_LOG)
    except Exception as e:
        print("writing crash log failed:", e)


def log_exception(where, e):
    text = format_exception(where, e)
    print(text)
    write(text)
