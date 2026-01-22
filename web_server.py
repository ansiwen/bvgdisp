import asyncio
import json
import settings


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
        input[type="number"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }
        input[type="text"]:focus,
        input[type="password"]:focus,
        input[type="number"]:focus {
            outline: none;
            border-color: #4CAF50;
        }
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 8px;
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
    </style>
</head>
<body>
    <div class="container">
        <h1>BVG Display Settings</h1>
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
    </div>

    <script>
        let searchTimeout = null;
        let currentSettings = {};

        // Load current settings on page load
        async function loadSettings() {
            try {
                const response = await fetch('/api/settings');
                const settings = await response.json();
                currentSettings = settings;

                document.getElementById('wifi-ssid').value = settings.WIFI_SSID || '';
                document.getElementById('wifi-password').value = settings.WIFI_PASSWORD || '';
                document.getElementById('api-url').value = settings.API_URL || 'https://v6.bvg.transport.rest';
                document.getElementById('station-id').value = settings.STATION_ID || '900100003';
                document.getElementById('selected-station-id').textContent = settings.STATION_ID || 'None';
                document.getElementById('walk-delay').value = settings.WALK_DELAY || 0;
                document.getElementById('colored').checked = settings.COLORED || false;
                document.getElementById('subway-colors').checked = settings.SUBWAY_COLORS || false;

                // Populate filtered lines
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
                COLORED: document.getElementById('colored').checked,
                SUBWAY_COLORS: document.getElementById('subway-colors').checked
            };

            try {
                const response = await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(settings)
                });

                if (response.ok) {
                    showMessage('Settings saved successfully! Restart the device to apply changes.', 'success');
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

        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + type;
            msg.style.display = 'block';
            setTimeout(() => { msg.style.display = 'none'; }, 5000);
        }

        // Restart button handler
        document.getElementById('restart-btn').addEventListener('click', async function() {
            if (!confirm('Are you sure you want to restart the device? This will apply any saved settings.')) {
                return;
            }

            const restartBtn = document.getElementById('restart-btn');
            restartBtn.disabled = true;
            restartBtn.textContent = 'Restarting...';

            try {
                await fetch('/api/restart', { method: 'POST' });
                showMessage('Device is restarting...', 'success');
            } catch (error) {
                // Expected - connection will be lost during restart
                showMessage('Device is restarting...', 'success');
            } finally {
                restartBtn.disabled = false;
                restartBtn.textContent = 'Restart Device';
            }
        });

        // Load settings on page load
        loadSettings();
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

        elif path == '/api/settings' and method == 'GET':
            # Read current settings
            await send_response(writer, "200 OK", "application/json", json.dumps(settings.get()))

        elif path == '/api/settings' and method == 'POST':
            # Save settings
            try:
                new_settings = json.loads(body)
                settings.set(new_settings)
                await send_response(writer, "200 OK", "application/json", '{"status":"ok"}')
            except Exception as e:
                print("saving settings failed:", e)
                await send_response(writer, "500 Internal Server Error", "text/plain", str(e))

        elif path == '/api/restart' and method == 'POST':
            # Restart device
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
