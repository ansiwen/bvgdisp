# Measures saved API responses, in the MicroPython unix port, for sizing
# _TEXT_BUF_SZ, _WARN_BUF_SZ and _RECV_BUF_SZ:
#
#   curl -o resp.json "http://vbb.a6n.de/stops/900100003/departures?results=14&duration=60&pretty=false&sparse=true&language=de"
#   micropython tools/measure_responses.py resp.json [more.json ...]
#
# It prints the widest destinations and warnings as the display renders
# them, and the heap the parsed JSON takes. That's 64-bit here, so expect
# roughly half on the device.
import sys, json, gc, micropython
# the repo root: the parent of this script's directory (no os.path here)
ROOT = (sys.argv[0].rsplit("/", 1)[0] if "/" in sys.argv[0] else ".") + "/.."
sys.path.insert(0, ROOT + "/src")
import font_bvg
src = open(ROOT + "/src/bvgdisp.py").read()
g = {"micropython": micropython, "font_bvg": font_bvg}
a = src.index("@micropython.native\ndef render_text")
exec(src[a:src.index("\n\n\n", a)], g)
a = src.index("def clean_name")
exec(src[a:src.index("\ndef ", a + 10)], g)
render_text, clean_name = g["render_text"], g["clean_name"]

dests, warns = {}, {}
for f in sys.argv[1:]:
    raw = open(f, "rb").read()
    gc.collect()
    m = gc.mem_alloc()
    d = json.loads(raw)
    gc.collect()
    print("%-30s %6d B response, %6d B parsed" % (f, len(raw), gc.mem_alloc() - m))
    for dep in d["departures"]:
        if dep.get("direction"):
            n = clean_name(dep["direction"])
            dests[n] = render_text(n + " - ", kerning=True)
    for w in d.get("warnings") or ():
        # as pick_warning() builds the ticker text
        msg = "%s: %s *** " % (w["summary"], clean_name(w["text"].split("\n")[0]))
        warns[msg] = render_text(msg, kerning=True)
    del d, raw
print("widest destinations (with \" - \"):")
for n, w in sorted(dests.items(), key=lambda x: -x[1])[:5]:
    print("  %4d px  %s" % (w, n))
print("widest warnings (%d):" % len(warns))
for n, w in sorted(warns.items(), key=lambda x: -x[1])[:5]:
    print("  %4d px  %s" % (w, n[:80]))
