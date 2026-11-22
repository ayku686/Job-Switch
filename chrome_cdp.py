#!/usr/bin/env python3
"""
Minimal Chrome DevTools Protocol client — stdlib only, no pip installs.

Windows/Linux replacement for the macOS AppleScript bridge. Chrome must be
started with:

    chrome.exe --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\ChromeScrape"

The dedicated --user-data-dir is NOT optional: Chrome 136+ silently ignores
--remote-debugging-port on the default profile. Log into LinkedIn once inside
that profile; the login persists across relaunches.
"""
import base64, json, os, socket, struct, urllib.request

DEBUG_HOST = "127.0.0.1"
DEBUG_PORT = 9222


class CDPError(RuntimeError):
    pass


class _WebSocket:
    """Just enough RFC 6455 for localhost CDP: client-masked text frames,
    fragmentation, ping/pong. No TLS, no extensions."""

    def __init__(self, url, timeout=120):
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (f"GET /{path} HTTP/1.1\r\n"
             f"Host: {hostport}\r\n"
             "Upgrade: websocket\r\n"
             "Connection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\n"
             "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake: connection closed")
            resp += chunk
        status = resp.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise CDPError(f"websocket handshake refused: {status.decode(errors='replace')}")

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise CDPError("websocket: connection closed mid-frame")
            buf += chunk
        return buf

    def send_text(self, text):
        payload = text.encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n < 126:
            head = struct.pack(">BB", 0x81, 0x80 | n)
        elif n < 65536:
            head = struct.pack(">BBH", 0x81, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x81, 0x80 | 127, n)
        self.sock.sendall(head + mask + masked)

    def recv_text(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else b""
            data = self._read(n)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            if op == 8:                              # close
                raise CDPError("websocket closed by Chrome")
            if op == 9:                              # ping -> masked empty pong
                self.sock.sendall(struct.pack(">BB", 0x8A, 0x80) + os.urandom(4))
                continue
            if op == 10:                             # pong
                continue
            msg += data
            if fin:
                return msg.decode("utf-8", errors="replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _http_json(path, timeout=10):
    with urllib.request.urlopen(f"http://{DEBUG_HOST}:{DEBUG_PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def linkedin_tab():
    """ws debugger URL of the first LinkedIn tab, or None if no such tab.
    Raises CDPError if Chrome itself is unreachable on the debug port."""
    try:
        targets = _http_json("/json/list")
    except OSError as e:
        raise CDPError(
            "cannot reach Chrome on port 9222.\n"
            "Start Chrome with:\n"
            '  --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\\ChromeScrape"\n'
            "(the separate user-data-dir is required: Chrome 136+ ignores the "
            f"debug port on the default profile)\n[{e}]")
    for t in targets:
        if t.get("type") == "page" and "linkedin.com" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    return None


def _rpc(ws_url, method, params, timeout):
    """One request per connection: survives renderer restarts and navigations,
    and DevTools being opened on the tab between calls."""
    ws = _WebSocket(ws_url, timeout=timeout)
    try:
        ws.send_text(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv_text())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise CDPError(msg["error"].get("message", str(msg["error"])))
                return msg.get("result", {})
    finally:
        ws.close()


def evaluate(ws_url, js, timeout=120):
    res = _rpc(ws_url, "Runtime.evaluate",
               {"expression": js, "returnByValue": True}, timeout)
    exc = res.get("exceptionDetails")
    if exc:
        detail = (exc.get("exception") or {}).get("description") or exc.get("text", "JS exception")
        return f"JS_ERROR: {detail[:300]}"
    val = res.get("result", {}).get("value")
    return "" if val is None else (val if isinstance(val, str) else json.dumps(val))


def navigate(ws_url, url, timeout=30):
    _rpc(ws_url, "Page.navigate", {"url": url}, timeout)
