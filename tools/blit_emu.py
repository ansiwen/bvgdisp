#!/usr/bin/env python3
"""Checks blit_asm(), the assembler loop of blit(), in an ARM emulator:

    tools/.deps/venv/bin/python tools/blit_emu.py [file.py]

The unix port can't run asm_thumb, so this runs the machine code that
mpy-cross generates from src/bvgdisp.py (or file.py) in unicorn:
1. the code must be the same for the RP2040 (armv6m) and the RP2350
2. it must decode as Thumb-1 for the Cortex-M0 (needs llvm). unicorn's
   Cortex-M0 model also runs Thumb-2, so it can't show that
3. as Cortex-M0 and as M33, it must give the same frame buffer as a Python
   reference, in random cases: all alignments, lengths 0-128
4. it prints the executed instructions per pixel on rows built from real
   station names

The parameter block is built like blit() builds it; keep the two in sync.
"""
import ast
import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpycode

try:
    from unicorn import Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
    from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_SP, UC_ARM_REG_LR,
                                   UC_CPU_ARM_CORTEX_M0, UC_CPU_ARM_CORTEX_M33)
except ImportError:
    sys.exit("unicorn is missing: run tools/setup.sh, then use tools/.deps/venv/bin/python")

CODE, RAM = 0x10000000, 0x20000000
RET = CODE + 0x9000
PARAMS, SRC, DST, STACK = RAM + 0x500, RAM + 0x10000, RAM + 0x40000, RAM + 0x1F0000
FB_W, FB_H = 128, 64
FB = FB_W * FB_H


class Emu:
    def __init__(self, code, model):
        uc = self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        uc.ctl_set_cpu_model(model)
        uc.mem_map(CODE, 0x100000)
        uc.mem_map(RAM, 0x200000)
        uc.mem_write(CODE, code)
        self.count = 0
        uc.hook_add(UC_HOOK_CODE, self._count)

    def _count(self, uc, addr, size, _):
        self.count += 1

    def blit(self, src, pen, dst_off, dst_width, src_off, src_width, length):
        """Like blit(): returns the frame buffer, counts the instructions"""
        shift = src_off & 31
        params = [DST + dst_off * 4, SRC + (src_off >> 5) * 4, 8, shift, 32 - shift,
                  length, dst_width * 4, src_width >> 3]
        uc = self.uc
        uc.mem_write(PARAMS, struct.pack("<8I", *(p & 0xFFFFFFFF for p in params)))
        uc.mem_write(SRC, src)
        uc.mem_write(DST, bytes(FB * 4))
        uc.reg_write(UC_ARM_REG_R0, PARAMS)
        uc.reg_write(UC_ARM_REG_R1, pen)
        uc.reg_write(UC_ARM_REG_SP, STACK)
        uc.reg_write(UC_ARM_REG_LR, RET | 1)
        uc.emu_start(CODE | 1, RET)
        return list(struct.unpack("<%dI" % FB, uc.mem_read(DST, FB * 4)))


def reference(src, pen, dst_off, dst_width, src_off, src_width, length):
    """What blit() must draw: the lit pixels, in pen"""
    out = [0] * FB
    stride = src_width // 8
    for r in range(8):
        for i in range(length):
            x = src_off + i
            if src[r * stride + (x >> 3)] >> (x & 7) & 1:
                out[dst_off + r * dst_width + i] = pen
    return out


def text_buffer(texts, width):
    """A 1-bit text buffer (make_textbuf()) with texts at the given x.
    Returns it and where the last text ends."""
    path = os.path.join(mpycode.ROOT, "font", "font_small.py")
    font = ast.literal_eval(ast.parse(open(path, encoding="utf-8").read()).body[1].value)
    buf = bytearray(width + 4)
    for x0, s, bold in texts:
        x = x0
        for ch in s:
            g = (bold and font.get("*" + ch)) or font.get(ch)
            if not g:
                continue
            n = g[0]
            for c in range(n - 1):
                for r in range(8):
                    if g[1 + r] >> (n - 1 - c) & 1 and x + c < width:
                        buf[r * (width // 8) + ((x + c) >> 3)] |= 1 << ((x + c) & 7)
            x += n
    return bytes(buf), x


def main():
    py_file = sys.argv[1] if len(sys.argv) > 1 else os.path.join(mpycode.ROOT, "src", "bvgdisp.py")
    code = {arch: mpycode.functions(py_file, arch)["blit_asm"] for arch in mpycode.ARCHS}
    ok = True

    same = code["armv6m"] == code["armv7emsp"]
    print("1. same machine code for armv6m and armv7emsp (%d bytes): %s" % (len(code["armv6m"]), same))
    ok &= same

    text = mpycode.disassemble(code["armv6m"], "armv6m")
    if text is None:
        print("2. skipped: llvm-mc and llvm-objdump are missing")
    else:
        lines = [l for l in text.splitlines() if l.strip()]
        bad = [l for l in lines if "unknown" in l or len(l.split("\t")[0].split(":")[1].split()) > 1]
        print("2. %d instructions, %d not 16-bit Thumb-1 for the Cortex-M0" % (len(lines), len(bad)))
        ok &= not bad

    rnd = random.Random(1)
    for model, name in ((UC_CPU_ARM_CORTEX_M0, "Cortex-M0"), (UC_CPU_ARM_CORTEX_M33, "Cortex-M33")):
        emu = Emu(code["armv6m"], model)
        bad = 0
        for t in range(300):
            src_width = rnd.choice((512, 4096))
            length = (rnd.choice((0, 1, 7, 8, 31, 32, 33, 63, 64, 95, 96, 127, 128)) if t % 2
                      else rnd.randint(0, 128))
            src_off = rnd.randint(0, src_width - length)
            dst_off = rnd.randint(0, FB_W - length) + FB_W * rnd.randint(0, FB_H - 8)
            density = rnd.choice((0.0, 0.1, 0.3, 0.9))
            src = bytes(sum((rnd.random() < density) << k for k in range(8)) for _ in range(src_width + 4))
            pen = rnd.randint(1, 0xFFFFFF)
            args = (src, pen, dst_off, FB_W, src_off, src_width, length)
            if emu.blit(*args) != reference(*args):
                bad += 1
                if bad <= 3:
                    print("   mismatch:", dict(src_width=src_width, length=length, src_off=src_off, dst_off=dst_off))
        print("3. %s: 300 random cases, %d mismatches" % (name, bad))
        ok &= not bad

    # rows like the display's: ETA 11 px, LINE 20 px, DEST at x = 31
    names = ["Hönow", "S+U Warschauer Str.", "Ruhleben", "S+U Hauptbahnhof via Perleberger Brücke",
             "Flughafen BER", "Hohenschönhausen, Zingster Str.", "S Spandau", "Betriebshof Marzahn via S Marzahn",
             "Michelangelostr.", "Ersatzverkehr Richtung Schillerstr.", "U Wittenbergplatz", "S+U Zoologischer Garten"]
    lines = ["U5", "M10", "S41", "RE1", "X9", "200", "N5", "FEX", "U2", "S7"]
    emu = Emu(code["armv6m"], UC_CPU_ARM_CORTEX_M33)
    pixels = 0
    emu.count = 0
    for k, name in enumerate(names):
        buf, end = text_buffer([(2, "%d'" % (k + 1), False), (11, lines[k % len(lines)], True),
                                (31, name + " - ", False)], 512)
        dest_sz = end - 31
        row = (k % 7) * 9 * FB_W
        calls = [(row + FB_W - 11, 0, 11), (row, 11, 20)]
        if dest_sz <= 96:
            calls.append((row + 23, 31, dest_sz))
        else:  # scrolling through the name, as the display does
            calls += [(row + 23, 31 + x, 96) for x in range(0, dest_sz - 96, 6)]
        for dst_off, src_off, length in calls:
            args = (buf, 0xFFAA00, dst_off, FB_W, src_off, 512, length)
            assert emu.blit(*args) == reference(*args)
            pixels += 8 * length
    print("4. %.2f instructions per pixel (%d pixels; plus the blit() wrapper per call)"
          % (emu.count / pixels, pixels))

    print("OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


main()
