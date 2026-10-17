#!/usr/bin/env python3
"""Disassemble what mpy-cross generates for a native, viper or asm_thumb
function, as on the device:

    python3 tools/disasm.py [--arch armv7emsp|armv6m] [file.py] [function]

The arch defaults to armv7emsp (RP2350); armv6m is the RP2040, where
instructions it lacks show as undecodable. Without a function, it lists
them with their code sizes. Read the hot loops before and after changing
one: docs/performance.md says what to look for.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpycode

ap = argparse.ArgumentParser()
ap.add_argument("--arch", default="armv7emsp", choices=sorted(mpycode.ARCHS))
ap.add_argument("file", nargs="?", default=os.path.join(mpycode.ROOT, "src", "bvgdisp.py"))
ap.add_argument("function", nargs="?")
args = ap.parse_args()

funcs = mpycode.functions(args.file, args.arch)
if not args.function:
    for name, code in funcs.items():
        print("%6d bytes  %s" % (len(code), name))
    sys.exit()
if args.function not in funcs:
    sys.exit("no native, viper or asm_thumb function %s in %s" % (args.function, args.file))
text = mpycode.disassemble(funcs[args.function], args.arch)
if text is None:
    sys.exit("llvm-mc and llvm-objdump are needed (Ubuntu: apt install llvm)")
print(text, end="")
