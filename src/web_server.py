import asyncio
import json
import hashlib
import os
import time
import settings

# Session management
_sessions = {}  # token -> last_activity_time
_nonces = {}  # nonce -> expiry_time
SESSION_TIMEOUT = 600  # 10 minutes in seconds
NONCE_TIMEOUT = 60  # 1 minute for nonce validity
MAX_BODY_SIZE = 4096  # settings JSON is well below 1 KB
_CHUNK_SIZE = const(1024)  # bytes per write() when sending responses


def _generate_hex(length=16):
    """Generate random hex string"""
    return "".join("%02x" % b for b in os.urandom(length))


def _sha256_hex(data):
    """Compute SHA256 and return hex string"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    digest = hashlib.sha256(data).digest()
    return "".join("%02x" % b for b in digest)


def _cleanup_expired():
    """Remove expired sessions and nonces"""
    now = time.time()
    expired_sessions = [t for t, ts in _sessions.items() if now - ts > SESSION_TIMEOUT]
    for t in expired_sessions:
        del _sessions[t]
    expired_nonces = [n for n, exp in _nonces.items() if now > exp]
    for n in expired_nonces:
        del _nonces[n]


def create_challenge():
    """Create challenge with nonce and salt for authentication"""
    _cleanup_expired()
    nonce = _generate_hex(16)
    salt = settings.get("PASSWORD_SALT")
    _nonces[nonce] = time.time() + NONCE_TIMEOUT
    return nonce, salt


def verify_login(nonce, response):
    """Verify challenge-response and return session token if valid"""
    _cleanup_expired()

    # Check nonce exists and not expired
    if nonce not in _nonces:
        return None

    # Remove nonce (single use)
    del _nonces[nonce]

    # Get stored password hash
    password_hash = settings.get("PASSWORD_HASH")

    # Compute expected response: SHA256(password_hash + nonce)
    expected = _sha256_hex(password_hash + nonce)

    if response != expected:
        return None

    # Create session
    token = _generate_hex(24)
    _sessions[token] = time.time()
    return token


def verify_session(token):
    """Verify session token and update activity time"""
    _cleanup_expired()

    if not token or token not in _sessions:
        return False

    _sessions[token] = time.time()
    return True


def logout(token):
    """Invalidate session"""
    if token in _sessions:
        del _sessions[token]


def change_password(nonce, old_response, encrypted_new_hash, new_salt):
    """Change password with encrypted new hash and new salt"""
    _cleanup_expired()

    # Check nonce
    if nonce not in _nonces:
        return False, "Invalid nonce"

    del _nonces[nonce]

    # Verify old password
    password_hash = settings.get("PASSWORD_HASH")
    expected = _sha256_hex(password_hash + nonce)

    if old_response != expected:
        return False, "Invalid password"

    # Validate new_salt format (should be 32 hex chars)
    if not new_salt or len(new_salt) != 32:
        return False, "Invalid salt"

    # Derive key: SHA256(password_hash + nonce + "newpass")
    key = _sha256_hex(password_hash + nonce + "newpass")

    # XOR decrypt new hash
    new_hash = "".join(
        "%02x" % (int(encrypted_new_hash[i : i + 2], 16) ^ int(key[i : i + 2], 16))
        for i in range(0, 64, 2)
    )

    # Save new password hash and salt
    settings.set({"PASSWORD_HASH": new_hash, "PASSWORD_SALT": new_salt})
    return True, "Password changed"


async def parse_request(reader):
    """Parse HTTP request and return method, path, headers, and body"""
    request_line = await reader.readline()
    request_line = request_line.decode("utf-8").strip()

    if not request_line:
        return None, None, {}, None

    parts = request_line.split(" ")
    if len(parts) < 2:
        return None, None, {}, None

    method = parts[0]
    path = parts[1]

    # Parse headers
    headers = {}
    while True:
        line = await reader.readline()
        line = line.decode("utf-8").strip()
        if not line:
            break
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    # Read body if present
    body = None
    if "content-length" in headers:
        content_length = int(headers["content-length"])
        if content_length > MAX_BODY_SIZE:
            raise ValueError("request body too large: %d" % content_length)
        if content_length > 0:
            # read() may return a partial body if it spans TCP segments
            body = await reader.readexactly(content_length)
            body = body.decode("utf-8")

    return method, path, headers, body


def _write_header(writer, status, content_type, length):
    writer.write((
        "HTTP/1.1 %s\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %d\r\n"
        "Connection: close\r\n"
        "\r\n" % (status, content_type, length)
    ).encode("utf-8"))


async def send_response(writer, status, content_type, body):
    """Send HTTP response"""
    if isinstance(body, str):
        body = body.encode("utf-8")
    _write_header(writer, status, content_type, len(body))
    # Write in slices: when the socket takes only part of a write(), asyncio
    # copies the unsent rest in one piece, which can fail on a fragmented
    # heap for big bodies.
    mv = memoryview(body)
    for i in range(0, len(body), _CHUNK_SIZE):
        writer.write(mv[i:i + _CHUNK_SIZE])
        await writer.drain()
    await writer.drain()


async def send_file(writer, path, content_type):
    """Send a file from flash, so it never has to fit in RAM as a whole"""
    try:
        size = os.stat(path)[6]
    except OSError:
        await send_response(writer, "500 Internal Server Error", "text/plain",
                            path + " is missing on the device")
        return
    _write_header(writer, "200 OK", content_type, size)
    buf = bytearray(_CHUNK_SIZE)
    mv = memoryview(buf)
    with open(path, "rb") as f:
        while True:
            n = f.readinto(buf)
            if not n:
                break
            # safe to reuse buf afterwards: write() copies what the socket
            # doesn't take, and drain() empties that copy
            writer.write(mv[:n])
            await writer.drain()
    await writer.drain()


async def handle_client(reader, writer):
    """Handle incoming HTTP requests"""
    try:
        method, path, headers, body = await parse_request(reader)

        if not method:
            writer.close()
            await writer.wait_closed()
            return

        print(f"Request: {method} {path}")

        # Route handling
        if path == "/" or path == "/index.html":
            await send_file(writer, "index.html", "text/html; charset=utf-8")

        elif path == "/api/auth/challenge" and method == "GET":
            # Generate challenge nonce and return salt
            nonce, salt = create_challenge()
            await send_response(
                writer,
                "200 OK",
                "application/json",
                json.dumps({"nonce": nonce, "salt": salt}),
            )

        elif path == "/api/auth/login" and method == "POST":
            # Verify login
            try:
                data = json.loads(body)
                token = verify_login(data.get("nonce"), data.get("response"))
                if token:
                    await send_response(
                        writer,
                        "200 OK",
                        "application/json",
                        json.dumps({"session": token}),
                    )
                else:
                    await send_response(
                        writer,
                        "401 Unauthorized",
                        "application/json",
                        '{"error":"Invalid credentials"}',
                    )
            except Exception as e:
                print(f"Login error: {e}")
                await send_response(
                    writer,
                    "400 Bad Request",
                    "application/json",
                    '{"error":"Invalid request"}',
                )

        elif path == "/api/auth/logout" and method == "POST":
            # Logout
            token = headers.get("x-session")
            logout(token)
            await send_response(writer, "200 OK", "application/json", '{"status":"ok"}')

        elif path == "/api/auth/password" and method == "POST":
            # Change password
            token = headers.get("x-session")
            if not verify_session(token):
                await send_response(
                    writer,
                    "401 Unauthorized",
                    "application/json",
                    '{"error":"Not authenticated"}',
                )
            else:
                try:
                    data = json.loads(body)
                    success, msg = change_password(
                        data.get("nonce"),
                        data.get("old_response"),
                        data.get("encrypted_new_hash"),
                        data.get("new_salt"),
                    )
                    if success:
                        await send_response(
                            writer, "200 OK", "application/json", '{"status":"ok"}'
                        )
                    else:
                        await send_response(
                            writer,
                            "400 Bad Request",
                            "application/json",
                            json.dumps({"error": msg}),
                        )
                except Exception as e:
                    print(f"Password change error: {e}")
                    await send_response(
                        writer,
                        "400 Bad Request",
                        "application/json",
                        '{"error":"Invalid request"}',
                    )

        elif path == "/api/settings" and method == "GET":
            # Read current settings (requires auth)
            token = headers.get("x-session")
            if not verify_session(token):
                await send_response(
                    writer,
                    "401 Unauthorized",
                    "application/json",
                    '{"error":"Not authenticated"}',
                )
            else:
                # Don't expose password hash or salt
                all_settings = settings.get()
                all_settings.pop("PASSWORD_HASH", None)
                all_settings.pop("PASSWORD_SALT", None)
                await send_response(
                    writer, "200 OK", "application/json", json.dumps(all_settings)
                )

        elif path == "/api/settings" and method == "POST":
            # Save settings (requires auth)
            token = headers.get("x-session")
            if not verify_session(token):
                await send_response(
                    writer,
                    "401 Unauthorized",
                    "application/json",
                    '{"error":"Not authenticated"}',
                )
            else:
                try:
                    new_settings = json.loads(body)
                    # Don't allow setting password through this endpoint
                    new_settings.pop("PASSWORD_HASH", None)
                    new_settings.pop("PASSWORD_SALT", None)
                    settings.set(new_settings)
                    await send_response(
                        writer, "200 OK", "application/json", '{"status":"ok"}'
                    )
                except Exception as e:
                    print("saving settings failed:", e)
                    await send_response(
                        writer, "500 Internal Server Error", "text/plain", str(e)
                    )

        elif path == "/api/restart" and method == "POST":
            # Restart device (requires auth)
            token = headers.get("x-session")
            if not verify_session(token):
                await send_response(
                    writer,
                    "401 Unauthorized",
                    "application/json",
                    '{"error":"Not authenticated"}',
                )
            else:
                await send_response(
                    writer, "200 OK", "application/json", '{"status":"restarting"}'
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                import machine

                await asyncio.sleep(0.5)  # Give time for response to be sent
                machine.reset()

        else:
            await send_response(writer, "404 Not Found", "text/plain", "Not Found")

    except Exception as e:
        print(f"Error handling request: {e}")
        import sys

        sys.print_exception(e)

    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except:
            pass


async def start_web_server(port=80):
    """Start the web server"""
    print(f"Starting web server on port {port}...")
    server = await asyncio.start_server(handle_client, "0.0.0.0", port)
    print(f"Web server running on http://0.0.0.0:{port}")
    return server
