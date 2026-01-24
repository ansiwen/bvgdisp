import asyncio
import json
import hashlib
import os
import time
import settings

# Session management
_sessions = {}  # token -> last_activity_time
_nonces = {}    # nonce -> expiry_time
SESSION_TIMEOUT = 600  # 10 minutes in seconds
NONCE_TIMEOUT = 60     # 1 minute for nonce validity


def _generate_hex(length=16):
    """Generate random hex string"""
    return ''.join('%02x' % b for b in os.urandom(length))


def _sha256_hex(data):
    """Compute SHA256 and return hex string"""
    if isinstance(data, str):
        data = data.encode('utf-8')
    digest = hashlib.sha256(data).digest()
    return ''.join('%02x' % b for b in digest)


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
    salt = settings.get('PASSWORD_SALT')
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
    password_hash = settings.get('PASSWORD_HASH')

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
    password_hash = settings.get('PASSWORD_HASH')
    expected = _sha256_hex(password_hash + nonce)

    if old_response != expected:
        return False, "Invalid password"

    # Validate new_salt format (should be 32 hex chars)
    if not new_salt or len(new_salt) != 32:
        return False, "Invalid salt"

    # Derive key: SHA256(password_hash + nonce + "newpass")
    key = _sha256_hex(password_hash + nonce + "newpass")

    # XOR decrypt new hash
    new_hash = ''.join(
        '%02x' % (int(encrypted_new_hash[i:i+2], 16) ^ int(key[i:i+2], 16))
        for i in range(0, 64, 2)
    )

    # Save new password hash and salt
    settings.set({'PASSWORD_HASH': new_hash, 'PASSWORD_SALT': new_salt})
    return True, "Password changed"


HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BVG Display Settings</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
            background: #f5f5f5;
            padding: 20px;
        }
        .container {
            max-width: 600px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        h1 {
            color: #333;
            margin-bottom: 30px;
            font-size: 24px;
        }
        h2 {
            color: #555;
            margin: 30px 0 15px 0;
            font-size: 18px;
            border-top: 1px solid #eee;
            padding-top: 20px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            color: #555;
            font-weight: 500;
        }
        input[type="text"],
        input[type="password"],
        input[type="number"],
        input[type="time"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }
        input[type="text"]:focus,
        input[type="password"]:focus,
        input[type="number"]:focus,
        input[type="time"]:focus {
            outline: none;
            border-color: #4CAF50;
        }
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
        }
        .checkbox-group:last-child {
            margin-bottom: 0;
        }
        .checkbox-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }
        .checkbox-grid .checkbox-group {
            margin-bottom: 0;
        }
        input[type="checkbox"] {
            width: 18px;
            height: 18px;
            cursor: pointer;
        }
        button {
            background: #4CAF50;
            color: white;
            padding: 12px 24px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 16px;
            width: 100%;
            margin-top: 10px;
        }
        button:hover {
            background: #45a049;
        }
        button:disabled {
            background: #ccc;
            cursor: not-allowed;
        }
        .search-container {
            position: relative;
        }
        #search-results {
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            background: white;
            border: 1px solid #ddd;
            border-top: none;
            border-radius: 0 0 4px 4px;
            max-height: 300px;
            overflow-y: auto;
            z-index: 1000;
            display: none;
        }
        .search-result-item {
            padding: 10px;
            cursor: pointer;
            border-bottom: 1px solid #f0f0f0;
        }
        .search-result-item:hover {
            background: #f5f5f5;
        }
        .search-result-name {
            font-weight: 500;
            color: #333;
        }
        .search-result-id {
            font-size: 12px;
            color: #888;
        }
        .message {
            padding: 12px;
            border-radius: 4px;
            margin-bottom: 20px;
            display: none;
        }
        .message.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .message.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .small-text {
            font-size: 12px;
            color: #888;
            margin-top: 5px;
        }
        .filtered-input {
            display: flex;
            gap: 5px;
            margin-bottom: 5px;
        }
        .filtered-input input {
            flex: 1;
        }
        .filtered-input button {
            width: auto;
            padding: 8px 12px;
            margin: 0;
            background: #f44336;
        }
        .filtered-input button:hover {
            background: #da190b;
        }
        #add-filtered {
            background: #2196F3;
            margin-top: 5px;
        }
        #add-filtered:hover {
            background: #0b7dda;
        }
        .login-container {
            text-align: center;
        }
        .login-container h1 {
            margin-bottom: 20px;
        }
        #login-form {
            max-width: 300px;
            margin: 0 auto;
        }
        #login-btn {
            background: #2196F3;
        }
        #login-btn:hover {
            background: #0b7dda;
        }
        .header-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
        }
        .header-bar h1 {
            margin-bottom: 0;
        }
        #logout-btn {
            width: auto;
            margin: 0;
            padding: 8px 16px;
            background: #757575;
        }
        #logout-btn:hover {
            background: #616161;
        }
        #change-password-btn {
            background: #9c27b0;
            margin-top: 10px;
        }
        #change-password-btn:hover {
            background: #7b1fa2;
        }
        .hidden {
            display: none !important;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Login View -->
        <div id="login-view" class="login-container">
            <h1>BVG Display Login</h1>
            <div id="login-message" class="message"></div>
            <form id="login-form">
                <div class="form-group">
                    <label for="login-password">Password</label>
                    <input type="password" id="login-password" required>
                </div>
                <button type="submit" id="login-btn">Login</button>
            </form>
            <div class="small-text" style="margin-top: 15px;">Default password: BVGdisp</div>
        </div>

        <!-- Settings View (hidden by default) -->
        <div id="settings-view" class="hidden">
            <div class="header-bar">
                <h1>BVG Display Settings</h1>
                <button id="logout-btn">Logout</button>
            </div>
            <div id="message" class="message"></div>

            <form id="settings-form">
                <div class="form-group">
                    <label for="wifi-ssid">WiFi SSID</label>
                    <input type="text" id="wifi-ssid" name="WIFI_SSID" required>
                </div>

                <div class="form-group">
                    <label for="wifi-password">WiFi Password</label>
                    <input type="password" id="wifi-password" name="WIFI_PASSWORD" required>
                </div>

                <div class="form-group">
                    <label for="api-url">API URL</label>
                    <input type="text" id="api-url" name="API_URL" required>
                </div>

                <div class="form-group">
                    <label for="station-search">Station Search</label>
                    <div class="search-container">
                        <input type="text" id="station-search" placeholder="Type to search for a station...">
                        <div id="search-results"></div>
                    </div>
                    <div class="small-text">Selected Station ID: <span id="selected-station-id">None</span></div>
                    <input type="hidden" id="station-id" name="STATION_ID">
                </div>

                <div class="form-group">
                    <label>Filtered Lines</label>
                    <div id="filtered-container"></div>
                    <button type="button" id="add-filtered">+ Add Line to Filter</button>
                    <div class="small-text">Lines to hide from display (e.g., U5, S8)</div>
                </div>

                <div class="form-group">
                    <label for="walk-delay">Walk Delay (seconds)</label>
                    <input type="number" id="walk-delay" name="WALK_DELAY" min="0" step="1" required>
                    <div class="small-text">Time in seconds to walk to the station</div>
                </div>

                <div class="form-group">
                    <label for="dest-offset">Destination Offset (pixels)</label>
                    <input type="number" id="dest-offset" name="DEST_OFFSET" min="0" step="1" required>
                    <div class="small-text">Horizontal offset where destination text starts</div>
                </div>

                <h2 style="margin-top: 20px;">Night Dimming</h2>
                <div class="form-group">
                    <label for="night-start">Night Start</label>
                    <input type="time" id="night-start" name="NIGHT_START" required>
                </div>
                <div class="form-group">
                    <label for="night-end">Night End</label>
                    <input type="time" id="night-end" name="NIGHT_END" required>
                </div>
                <div class="form-group">
                    <label for="night-dimming">Dimming Level (0-10)</label>
                    <input type="number" id="night-dimming" name="NIGHT_DIMMING" min="0" max="10" step="1" required>
                    <div class="small-text">0 = display off, 10 = no dimming</div>
                </div>

                <div class="form-group">
                    <label>Transport Types</label>
                    <div class="checkbox-grid">
                        <div class="checkbox-group">
                            <input type="checkbox" id="show-bus" name="SHOW_BUS">
                            <label for="show-bus">Bus</label>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="show-tram" name="SHOW_TRAM">
                            <label for="show-tram">Tram</label>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="show-subway" name="SHOW_SUBWAY">
                            <label for="show-subway">Subway (U-Bahn)</label>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="show-suburban" name="SHOW_SUBURBAN">
                            <label for="show-suburban">Suburban (S-Bahn)</label>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="show-regional" name="SHOW_REGIONAL">
                            <label for="show-regional">Regional (RE/RB)</label>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="show-express" name="SHOW_EXPRESS">
                            <label for="show-express">Express (IC/ICE)</label>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="show-ferry" name="SHOW_FERRY">
                            <label for="show-ferry">Ferry</label>
                        </div>
                    </div>
                </div>

                <div class="form-group">
                    <div class="checkbox-group">
                        <input type="checkbox" id="colored" name="COLORED">
                        <label for="colored">Use Colored Display</label>
                    </div>
                </div>

                <div class="form-group">
                    <div class="checkbox-group">
                        <input type="checkbox" id="subway-colors" name="SUBWAY_COLORS">
                        <label for="subway-colors">Use Subway Line Colors</label>
                    </div>
                </div>

                <button type="submit" id="save-btn">Save Settings</button>
            </form>

            <button id="restart-btn" style="background: #ff9800; margin-top: 20px;">Restart Device</button>

            <h2>Change Password</h2>
            <form id="password-form">
                <div class="form-group">
                    <label for="old-password">Current Password</label>
                    <input type="password" id="old-password" required>
                </div>
                <div class="form-group">
                    <label for="new-password">New Password</label>
                    <input type="password" id="new-password" required>
                </div>
                <div class="form-group">
                    <label for="confirm-password">Confirm New Password</label>
                    <input type="password" id="confirm-password" required>
                </div>
                <button type="submit" id="change-password-btn">Change Password</button>
            </form>
        </div>
    </div>

    <script>
        let searchTimeout = null;
        let currentSettings = {};
        let sessionToken = sessionStorage.getItem('session');

        // Pure JavaScript SHA256 implementation (works over HTTP)
        function sha256(message) {
            function rightRotate(value, amount) {
                return (value >>> amount) | (value << (32 - amount));
            }
            const mathPow = Math.pow;
            const maxWord = mathPow(2, 32);
            let result = '';
            const words = [];
            const asciiBitLength = message.length * 8;
            let hash = [];
            const k = [];
            let primeCounter = 0;
            const isComposite = {};
            for (let candidate = 2; primeCounter < 64; candidate++) {
                if (!isComposite[candidate]) {
                    for (let i = 0; i < 313; i += candidate) {
                        isComposite[i] = candidate;
                    }
                    hash[primeCounter] = (mathPow(candidate, .5) * maxWord) | 0;
                    k[primeCounter++] = (mathPow(candidate, 1 / 3) * maxWord) | 0;
                }
            }
            message += '\\x80';
            while (message.length % 64 - 56) message += '\\x00';
            for (let i = 0; i < message.length; i++) {
                const j = message.charCodeAt(i);
                if (j >> 8) return;
                words[i >> 2] |= j << ((3 - i) % 4) * 8;
            }
            words[words.length] = ((asciiBitLength / maxWord) | 0);
            words[words.length] = (asciiBitLength);
            for (let j = 0; j < words.length;) {
                const w = words.slice(j, j += 16);
                const oldHash = hash;
                hash = hash.slice(0, 8);
                for (let i = 0; i < 64; i++) {
                    const w15 = w[i - 15], w2 = w[i - 2];
                    const a = hash[0], e = hash[4];
                    const temp1 = hash[7]
                        + (rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25))
                        + ((e & hash[5]) ^ ((~e) & hash[6]))
                        + k[i]
                        + (w[i] = (i < 16) ? w[i] : (
                            w[i - 16]
                            + (rightRotate(w15, 7) ^ rightRotate(w15, 18) ^ (w15 >>> 3))
                            + w[i - 7]
                            + (rightRotate(w2, 17) ^ rightRotate(w2, 19) ^ (w2 >>> 10))
                        ) | 0);
                    const temp2 = (rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22))
                        + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]));
                    hash = [(temp1 + temp2) | 0].concat(hash);
                    hash[4] = (hash[4] + temp1) | 0;
                }
                for (let i = 0; i < 8; i++) {
                    hash[i] = (hash[i] + oldHash[i]) | 0;
                }
            }
            for (let i = 0; i < 8; i++) {
                for (let j = 3; j + 1; j--) {
                    const b = (hash[i] >> (j * 8)) & 255;
                    result += ((b < 16) ? '0' : '') + b.toString(16);
                }
            }
            return result;
        }

        // XOR two hex strings
        function xorHex(a, b) {
            let result = '';
            for (let i = 0; i < a.length; i += 2) {
                const byte = parseInt(a.substr(i, 2), 16) ^ parseInt(b.substr(i, 2), 16);
                result += byte.toString(16).padStart(2, '0');
            }
            return result;
        }

        // Generate random hex string
        function generateHex(bytes) {
            let result = '';
            for (let i = 0; i < bytes; i++) {
                result += Math.floor(Math.random() * 256).toString(16).padStart(2, '0');
            }
            return result;
        }

        // Authenticated fetch wrapper
        async function authFetch(url, options = {}) {
            if (!options.headers) options.headers = {};
            if (sessionToken) {
                options.headers['X-Session'] = sessionToken;
            }
            return fetch(url, options);
        }

        // Show/hide views
        function showLogin() {
            document.getElementById('login-view').classList.remove('hidden');
            document.getElementById('settings-view').classList.add('hidden');
        }

        function showSettings() {
            document.getElementById('login-view').classList.add('hidden');
            document.getElementById('settings-view').classList.remove('hidden');
            loadSettings();
        }

        function showLoginMessage(text, type) {
            const msg = document.getElementById('login-message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            msg.style.display = 'block';
            setTimeout(() => { msg.style.display = 'none'; }, 5000);
        }

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            msg.style.display = 'block';
            setTimeout(() => { msg.style.display = 'none'; }, 5000);
        }

        // Login handler
        document.getElementById('login-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            const password = document.getElementById('login-password').value;
            const loginBtn = document.getElementById('login-btn');

            loginBtn.disabled = true;
            loginBtn.textContent = 'Logging in...';

            try {
                // Get challenge
                const challengeResp = await fetch('/api/auth/challenge');
                const { nonce, salt } = await challengeResp.json();

                // Compute response: SHA256(SHA256(salt + password) + nonce)
                const passwordHash = sha256(salt + password);
                const response = sha256(passwordHash + nonce);

                // Send login request
                const loginResp = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ nonce, response })
                });

                if (loginResp.ok) {
                    const { session } = await loginResp.json();
                    sessionToken = session;
                    sessionStorage.setItem('session', session);
                    document.getElementById('login-password').value = '';
                    showSettings();
                } else {
                    showLoginMessage('Invalid password', 'error');
                }
            } catch (error) {
                showLoginMessage('Login failed: ' + error.message, 'error');
            } finally {
                loginBtn.disabled = false;
                loginBtn.textContent = 'Login';
            }
        });

        // Logout handler
        document.getElementById('logout-btn').addEventListener('click', async function() {
            try {
                await authFetch('/api/auth/logout', { method: 'POST' });
            } catch (e) {}
            sessionToken = null;
            sessionStorage.removeItem('session');
            showLogin();
        });

        // Check session on load
        async function checkSession() {
            if (!sessionToken) {
                showLogin();
                return;
            }

            try {
                const resp = await authFetch('/api/settings');
                if (resp.ok) {
                    showSettings();
                } else {
                    sessionToken = null;
                    sessionStorage.removeItem('session');
                    showLogin();
                }
            } catch (e) {
                showLogin();
            }
        }

        // Load current settings
        async function loadSettings() {
            try {
                const response = await authFetch('/api/settings');
                if (!response.ok) {
                    if (response.status === 401) {
                        sessionToken = null;
                        sessionStorage.removeItem('session');
                        showLogin();
                        return;
                    }
                    throw new Error('Failed to load settings');
                }
                const settings = await response.json();
                currentSettings = settings;

                document.getElementById('wifi-ssid').value = settings.WIFI_SSID || '';
                document.getElementById('wifi-password').value = settings.WIFI_PASSWORD || '';
                document.getElementById('api-url').value = settings.API_URL || 'https://v6.bvg.transport.rest';
                document.getElementById('station-id').value = settings.STATION_ID || '900100003';
                document.getElementById('selected-station-id').textContent = settings.STATION_ID || 'None';
                document.getElementById('walk-delay').value = settings.WALK_DELAY || 0;
                document.getElementById('dest-offset').value = settings.DEST_OFFSET || 20;
                document.getElementById('show-bus').checked = settings.SHOW_BUS !== false;
                document.getElementById('show-tram').checked = settings.SHOW_TRAM !== false;
                document.getElementById('show-subway').checked = settings.SHOW_SUBWAY !== false;
                document.getElementById('show-regional').checked = settings.SHOW_REGIONAL !== false;
                document.getElementById('show-suburban').checked = settings.SHOW_SUBURBAN !== false;
                document.getElementById('show-ferry').checked = settings.SHOW_FERRY !== false;
                document.getElementById('show-express').checked = settings.SHOW_EXPRESS !== false;
                document.getElementById('colored').checked = settings.COLORED || false;
                document.getElementById('subway-colors').checked = settings.SUBWAY_COLORS || false;
                document.getElementById('night-start').value = settings.NIGHT_START || '22:00';
                document.getElementById('night-end').value = settings.NIGHT_END || '06:00';
                document.getElementById('night-dimming').value = settings.NIGHT_DIMMING !== undefined ? settings.NIGHT_DIMMING : 3;

                // Clear and populate filtered lines
                document.getElementById('filtered-container').innerHTML = '';
                const filtered = settings.FILTERED || [];
                filtered.forEach(line => addFilteredLine(line));
            } catch (error) {
                showMessage('Failed to load settings: ' + error.message, 'error');
            }
        }

        // Station search functionality
        document.getElementById('station-search').addEventListener('input', function(e) {
            const query = e.target.value.trim();

            clearTimeout(searchTimeout);

            if (query.length < 2) {
                document.getElementById('search-results').style.display = 'none';
                return;
            }

            searchTimeout = setTimeout(() => searchStations(query), 300);
        });

        async function searchStations(query) {
            try {
                const response = await fetch(`http://a6n.de:33000/stops?query=${encodeURIComponent(query)}`);
                const data = await response.json();

                // Filter and transform results
                const stations = data
                    .filter(item => item.name && !item.id.includes('::'))
                    .map(item => {
                        const parts = item.id.split(':');
                        const stationId = parts[2];
                        return {
                            name: item.name,
                            id: stationId,
                            fullId: item.id
                        };
                    });

                const resultsDiv = document.getElementById('search-results');
                resultsDiv.innerHTML = '';

                if (stations.length === 0) {
                    resultsDiv.innerHTML = '<div class="search-result-item">No results found</div>';
                } else {
                    stations.forEach(station => {
                        const item = document.createElement('div');
                        item.className = 'search-result-item';
                        item.innerHTML = `
                            <div class="search-result-name">${station.name}</div>
                            <div class="search-result-id">ID: ${station.id}</div>
                        `;
                        item.onclick = () => selectStation(station);
                        resultsDiv.appendChild(item);
                    });
                }

                resultsDiv.style.display = 'block';
            } catch (error) {
                console.error('Search failed:', error);
            }
        }

        function selectStation(station) {
            document.getElementById('station-id').value = station.id;
            document.getElementById('selected-station-id').textContent = station.id + ' - ' + station.name;
            document.getElementById('station-search').value = station.name;
            document.getElementById('search-results').style.display = 'none';
        }

        // Close search results when clicking outside
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.search-container')) {
                document.getElementById('search-results').style.display = 'none';
            }
        });

        // Filtered lines management
        function addFilteredLine(value = '') {
            const container = document.getElementById('filtered-container');
            const div = document.createElement('div');
            div.className = 'filtered-input';
            div.innerHTML = `
                <input type="text" placeholder="Line name (e.g., U5)" value="${value}">
                <button type="button" onclick="this.parentElement.remove()">Remove</button>
            `;
            container.appendChild(div);
        }

        document.getElementById('add-filtered').addEventListener('click', () => addFilteredLine());

        // Form submission
        document.getElementById('settings-form').addEventListener('submit', async function(e) {
            e.preventDefault();

            const saveBtn = document.getElementById('save-btn');
            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving...';

            // Gather filtered lines
            const filteredInputs = document.querySelectorAll('#filtered-container input');
            const filtered = Array.from(filteredInputs)
                .map(input => input.value.trim())
                .filter(val => val.length > 0);

            const settings = {
                WIFI_SSID: document.getElementById('wifi-ssid').value,
                WIFI_PASSWORD: document.getElementById('wifi-password').value,
                API_URL: document.getElementById('api-url').value,
                STATION_ID: parseInt(document.getElementById('station-id').value),
                FILTERED: filtered,
                WALK_DELAY: parseInt(document.getElementById('walk-delay').value),
                DEST_OFFSET: parseInt(document.getElementById('dest-offset').value),
                SHOW_BUS: document.getElementById('show-bus').checked,
                SHOW_TRAM: document.getElementById('show-tram').checked,
                SHOW_SUBWAY: document.getElementById('show-subway').checked,
                SHOW_REGIONAL: document.getElementById('show-regional').checked,
                SHOW_SUBURBAN: document.getElementById('show-suburban').checked,
                SHOW_FERRY: document.getElementById('show-ferry').checked,
                SHOW_EXPRESS: document.getElementById('show-express').checked,
                COLORED: document.getElementById('colored').checked,
                SUBWAY_COLORS: document.getElementById('subway-colors').checked,
                NIGHT_START: document.getElementById('night-start').value,
                NIGHT_END: document.getElementById('night-end').value,
                NIGHT_DIMMING: parseInt(document.getElementById('night-dimming').value)
            };

            try {
                const response = await authFetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(settings)
                });

                if (response.ok) {
                    showMessage('Settings saved successfully! Restart the device to apply changes.', 'success');
                } else if (response.status === 401) {
                    sessionToken = null;
                    sessionStorage.removeItem('session');
                    showLogin();
                } else {
                    const error = await response.text();
                    showMessage('Failed to save settings: ' + error, 'error');
                }
            } catch (error) {
                showMessage('Failed to save settings: ' + error.message, 'error');
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save Settings';
            }
        });

        // Restart button handler
        document.getElementById('restart-btn').addEventListener('click', async function() {
            if (!confirm('Are you sure you want to restart the device? This will apply any saved settings.')) {
                return;
            }

            const restartBtn = document.getElementById('restart-btn');
            restartBtn.disabled = true;
            restartBtn.textContent = 'Restarting...';

            try {
                await authFetch('/api/restart', { method: 'POST' });
                showMessage('Device is restarting...', 'success');
            } catch (error) {
                // Expected - connection will be lost during restart
                showMessage('Device is restarting...', 'success');
            } finally {
                restartBtn.disabled = false;
                restartBtn.textContent = 'Restart Device';
            }
        });

        // Password change handler
        document.getElementById('password-form').addEventListener('submit', async function(e) {
            e.preventDefault();

            const oldPassword = document.getElementById('old-password').value;
            const newPassword = document.getElementById('new-password').value;
            const confirmPassword = document.getElementById('confirm-password').value;

            if (newPassword !== confirmPassword) {
                showMessage('New passwords do not match', 'error');
                return;
            }

            if (newPassword.length < 1) {
                showMessage('Password cannot be empty', 'error');
                return;
            }

            const changeBtn = document.getElementById('change-password-btn');
            changeBtn.disabled = true;
            changeBtn.textContent = 'Changing...';

            try {
                // Get challenge
                const challengeResp = await fetch('/api/auth/challenge');
                const { nonce, salt } = await challengeResp.json();

                // Compute old password response using current salt
                const oldPasswordHash = sha256(salt + oldPassword);
                const oldResponse = sha256(oldPasswordHash + nonce);

                // Generate new salt and compute new password hash
                const newSalt = generateHex(16);
                const newPasswordHash = sha256(newSalt + newPassword);

                // Encrypt new hash
                const key = sha256(oldPasswordHash + nonce + 'newpass');
                const encryptedNewHash = xorHex(newPasswordHash, key);

                // Send password change request
                const resp = await authFetch('/api/auth/password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ nonce, old_response: oldResponse, encrypted_new_hash: encryptedNewHash, new_salt: newSalt })
                });

                if (resp.ok) {
                    showMessage('Password changed successfully', 'success');
                    document.getElementById('old-password').value = '';
                    document.getElementById('new-password').value = '';
                    document.getElementById('confirm-password').value = '';
                } else {
                    const data = await resp.json();
                    showMessage(data.error || 'Failed to change password', 'error');
                }
            } catch (error) {
                showMessage('Failed to change password: ' + error.message, 'error');
            } finally {
                changeBtn.disabled = false;
                changeBtn.textContent = 'Change Password';
            }
        });

        // Check session on page load
        checkSession();
    </script>
</body>
</html>
"""


async def parse_request(reader):
    """Parse HTTP request and return method, path, headers, and body"""
    request_line = await reader.readline()
    request_line = request_line.decode('utf-8').strip()

    if not request_line:
        return None, None, {}, None

    parts = request_line.split(' ')
    if len(parts) < 2:
        return None, None, {}, None

    method = parts[0]
    path = parts[1]

    # Parse headers
    headers = {}
    while True:
        line = await reader.readline()
        line = line.decode('utf-8').strip()
        if not line:
            break
        if ':' in line:
            key, value = line.split(':', 1)
            headers[key.strip().lower()] = value.strip()

    # Read body if present
    body = None
    if 'content-length' in headers:
        content_length = int(headers['content-length'])
        if content_length > 0:
            body = await reader.read(content_length)
            body = body.decode('utf-8')

    return method, path, headers, body


async def send_response(writer, status, content_type, body):
    """Send HTTP response"""
    if isinstance(body, str):
        body = body.encode('utf-8')

    response = f"HTTP/1.1 {status}\r\n"
    response += f"Content-Type: {content_type}\r\n"
    response += f"Content-Length: {len(body)}\r\n"
    response += "Connection: close\r\n"
    response += "\r\n"

    writer.write(response.encode('utf-8'))
    writer.write(body)
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
        if path == '/' or path == '/index.html':
            await send_response(writer, "200 OK", "text/html", HTML_PAGE)

        elif path == '/api/auth/challenge' and method == 'GET':
            # Generate challenge nonce and return salt
            nonce, salt = create_challenge()
            await send_response(writer, "200 OK", "application/json", json.dumps({"nonce": nonce, "salt": salt}))

        elif path == '/api/auth/login' and method == 'POST':
            # Verify login
            try:
                data = json.loads(body)
                token = verify_login(data.get('nonce'), data.get('response'))
                if token:
                    await send_response(writer, "200 OK", "application/json", json.dumps({"session": token}))
                else:
                    await send_response(writer, "401 Unauthorized", "application/json", '{"error":"Invalid credentials"}')
            except Exception as e:
                print(f"Login error: {e}")
                await send_response(writer, "400 Bad Request", "application/json", '{"error":"Invalid request"}')

        elif path == '/api/auth/logout' and method == 'POST':
            # Logout
            token = headers.get('x-session')
            logout(token)
            await send_response(writer, "200 OK", "application/json", '{"status":"ok"}')

        elif path == '/api/auth/password' and method == 'POST':
            # Change password
            token = headers.get('x-session')
            if not verify_session(token):
                await send_response(writer, "401 Unauthorized", "application/json", '{"error":"Not authenticated"}')
            else:
                try:
                    data = json.loads(body)
                    success, msg = change_password(
                        data.get('nonce'),
                        data.get('old_response'),
                        data.get('encrypted_new_hash'),
                        data.get('new_salt')
                    )
                    if success:
                        await send_response(writer, "200 OK", "application/json", '{"status":"ok"}')
                    else:
                        await send_response(writer, "400 Bad Request", "application/json", json.dumps({"error": msg}))
                except Exception as e:
                    print(f"Password change error: {e}")
                    await send_response(writer, "400 Bad Request", "application/json", '{"error":"Invalid request"}')

        elif path == '/api/settings' and method == 'GET':
            # Read current settings (requires auth)
            token = headers.get('x-session')
            if not verify_session(token):
                await send_response(writer, "401 Unauthorized", "application/json", '{"error":"Not authenticated"}')
            else:
                # Don't expose password hash or salt
                all_settings = settings.get()
                all_settings.pop('PASSWORD_HASH', None)
                all_settings.pop('PASSWORD_SALT', None)
                await send_response(writer, "200 OK", "application/json", json.dumps(all_settings))

        elif path == '/api/settings' and method == 'POST':
            # Save settings (requires auth)
            token = headers.get('x-session')
            if not verify_session(token):
                await send_response(writer, "401 Unauthorized", "application/json", '{"error":"Not authenticated"}')
            else:
                try:
                    new_settings = json.loads(body)
                    # Don't allow setting password through this endpoint
                    new_settings.pop('PASSWORD_HASH', None)
                    new_settings.pop('PASSWORD_SALT', None)
                    settings.set(new_settings)
                    await send_response(writer, "200 OK", "application/json", '{"status":"ok"}')
                except Exception as e:
                    print("saving settings failed:", e)
                    await send_response(writer, "500 Internal Server Error", "text/plain", str(e))

        elif path == '/api/restart' and method == 'POST':
            # Restart device (requires auth)
            token = headers.get('x-session')
            if not verify_session(token):
                await send_response(writer, "401 Unauthorized", "application/json", '{"error":"Not authenticated"}')
            else:
                await send_response(writer, "200 OK", "application/json", '{"status":"restarting"}')
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
    server = await asyncio.start_server(handle_client, '0.0.0.0', port)
    print(f"Web server running on http://0.0.0.0:{port}")
    return server
