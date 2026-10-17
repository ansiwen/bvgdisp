# Checks the web server's challenge-response login and its bounded nonce
# and session tables, in the MicroPython unix port:
#
#   micropython tools/test_web.py
import sys, gc, os, hashlib, binascii
# the repo root: the parent of this script's directory (no os.path here)
ROOT = (sys.argv[0].rsplit("/", 1)[0] if "/" in sys.argv[0] else ".") + "/.."
sys.path.insert(0, ROOT + "/src")
import web_server as w, settings

def const_value(name):
    # _NAME = const(n) is compiled in, not a module attribute
    src = open(ROOT + "/src/web_server.py").read()
    head = name + " = const("
    i = src.index(head) + len(head)
    return int(src[i:src.index(")", i)])
MAX_NONCES = const_value("_MAX_NONCES")
MAX_SESSIONS = const_value("_MAX_SESSIONS")

def old_hex(b):
    return "".join("%02x" % x for x in b)
for n in (1, 16, 24, 32):
    b = os.urandom(n)
    assert binascii.hexlify(b).decode() == old_hex(b)
assert w._sha256_hex("abc") == old_hex(hashlib.sha256(b"abc").digest())

def login(nonce, password_hash=settings.get("PASSWORD_HASH")):
    return w.verify_login(nonce, w._sha256_hex(password_hash + nonce))

first, _ = w.create_challenge()
gc.collect()
a = gc.mem_alloc()
for _ in range(5000):
    w.create_challenge()
gc.collect()
print("after 5000 challenges: %d nonces, heap %+d B" % (len(w._nonces), gc.mem_alloc() - a))
assert len(w._nonces) <= MAX_NONCES
assert login(first) is None, "a displaced nonce must not work"
nonce, salt = w.create_challenge()
assert login(nonce, "wrong") is None, "a wrong password must not work"
assert login(nonce) is None, "a nonce is single use"
tokens = []
for _ in range(MAX_SESSIONS + 2):
    nonce, salt = w.create_challenge()
    t = login(nonce)
    assert t and len(t) == 48, t
    tokens.append(t)
assert len(w._sessions) == MAX_SESSIONS
assert not w.verify_session(tokens[0]) and not w.verify_session(tokens[1])
assert all(w.verify_session(t) for t in tokens[2:])
w.logout(tokens[-1])
assert not w.verify_session(tokens[-1])
print("OK")
