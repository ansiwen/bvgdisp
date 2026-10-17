"""The machine code that mpy-cross generates for a function, as on the device.

Used by disasm.py and blit_emu.py. Needs tools/setup.sh to have run.
"""
import os
import re
import subprocess
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
OUT = os.path.join(TOOLS, "out")
MP = os.path.join(TOOLS, ".deps", "micropython")
MPY_CROSS = os.path.join(MP, "mpy-cross", "build", "mpy-cross")
MPY_TOOL = os.path.join(MP, "tools", "mpy-tool.py")
QSTRDEFS = os.path.join(MP, "ports", "unix", "build-standard", "genhdr", "qstrdefs.preprocessed.h")
UNIX = os.path.join(MP, "ports", "unix", "build-standard", "micropython")

# -march for mpy-cross -> (llvm triple, llvm cpu)
ARCHS = {
    "armv7emsp": ("thumbv8m.main-none-eabi", "cortex-m33"),  # RP2350
    "armv6m": ("thumbv6m-none-eabi", "cortex-m0"),  # RP2040
}

DECORATED = re.compile(r"^@micropython\.(native|viper|asm_thumb)\s*\ndef (\w+)\(", re.M)


def need(path, what="tools/setup.sh"):
    if not os.path.exists(path):
        sys.exit("%s is missing, run %s first" % (path, what))


def functions(py_file, arch):
    """Return {name: machine code} of the native, viper and asm_thumb
    functions in py_file, compiled for arch."""
    need(MPY_CROSS)
    need(QSTRDEFS)
    os.makedirs(OUT, exist_ok=True)
    base = os.path.join(OUT, os.path.basename(py_file)[:-3] + "_" + arch)
    subprocess.run([MPY_CROSS, "-march=" + arch, "-o", base + ".mpy", py_file], check=True)
    c = subprocess.run([sys.executable, MPY_TOOL, "-f", "-q", QSTRDEFS, base + ".mpy"],
                       check=True, capture_output=True, text=True).stdout
    arrays = {}
    for m in re.finditer(r"static const byte (fun_data_\w+)\[\d+\][^=]*= \{(.*?)\};", c, re.S):
        arrays[m.group(1)] = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", m.group(2)))
    # mpy-tool names native code after the function, but viper and asm_thumb
    # code after the file (..._dot_py, ..._dot_py2, ...), in source order
    by_file = sorted((k for k in arrays if re.search(r"_dot_py\d*$", k)),
                     key=lambda k: int(re.search(r"(\d*)$", k).group(1) or 1))
    out = {}
    unnamed = iter(by_file)
    for kind, name in DECORATED.findall(open(py_file, encoding="utf-8").read()):
        if kind == "native":
            key = [k for k in arrays if k.endswith("_" + name)]
            if key:
                out[name] = arrays[key[0]]
        else:
            out[name] = arrays[next(unnamed)]
    return out


def llvm(tool):
    """Path of an llvm tool (llvm-mc, llvm-objdump), versioned or not."""
    for suffix in ("", "-20", "-19", "-18", "-17", "-16", "-15", "-14"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, tool + suffix)
            if os.access(p, os.X_OK):
                return p
    return None


def disassemble(code, arch):
    """Return the disassembly of code as text, or None without llvm. With
    arch "armv6m", instructions the RP2040 lacks don't decode."""
    mc, objdump = llvm("llvm-mc"), llvm("llvm-objdump")
    if not (mc and objdump):
        return None
    triple, cpu = ARCHS[arch]
    os.makedirs(OUT, exist_ok=True)
    asm, obj = os.path.join(OUT, "code.S"), os.path.join(OUT, "code.o")
    with open(asm, "w") as f:
        f.write(".syntax unified\n.thumb\n.text\nf:\n")
        for i in range(0, len(code), 16):
            f.write(".byte " + ",".join(str(b) for b in code[i:i + 16]) + "\n")
    subprocess.run([mc, "-triple=" + triple, "-mcpu=" + cpu, "-filetype=obj", asm, "-o", obj], check=True)
    text = subprocess.run([objdump, "-d", "--triple=" + triple, "--mcpu=" + cpu, obj],
                          check=True, capture_output=True, text=True).stdout
    return text[text.index("<f>:") + 5:]
