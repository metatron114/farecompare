"""零依赖 Edge 浏览器控制器（CDP over 手写 WebSocket）。

用途：用本机 Edge 打开去哪儿/携程等页面，等前端 JS 渲染完成后取回 DOM 或 JSON，
      全程本地计算，**不消耗任何 AI token**。

依赖：仅 Python 标准库（socket + json + ssl + subprocess）。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.request

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
from .paths import cache_dir

ROOT = os.path.dirname(cache_dir())


class WSError(RuntimeError):
    pass


class RawWebSocket:
    """最小可用的 WebSocket 客户端（仅客户端掩码帧，支持文本/分片/大帧）。"""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        assert url.startswith("ws://")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host = host
        self.port = int(port or 80)
        self.path = "/" + path
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._handshake()
        self._buf = b""

    def _handshake(self) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        # 注意：CDP 端点会拒绝带 Origin 头的握手（403），因此不发送 Origin
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("握手时连接被关闭")
            data += chunk
        head, _, tail = data.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise WSError(f"握手失败: {head.split(b'\r\n')[0]!r}")
        self._buf = tail

    # ---------- 帧读写 ----------
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WSError("连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> tuple[int, bytes]:
        b1, b2 = self._recv_exact(2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return opcode, payload, bool(fin)

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        """读取一条完整文本消息（自动拼接分片与 ping/pong）。"""
        parts: list[bytes] = []
        while True:
            opcode, payload, fin = self._read_frame()
            if opcode == 0x9:                     # ping -> pong
                self._pong(payload)
                continue
            if opcode == 0xA:                     # pong
                continue
            if opcode == 0x8:                     # close
                raise WSError("浏览器关闭了连接")
            parts.append(payload)
            if fin:
                break
        return b"".join(parts).decode("utf-8", "replace")

    def _pong(self, data: bytes) -> None:
        mask = secrets.token_bytes(4)
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + mask + payload)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class Browser:
    """Edge 进程 + CDP 会话。"""

    def __init__(self, port: int | None = None, headless: bool = True,
                 profile: str | None = None, timeout: float = 40.0) -> None:
        exe = next((p for p in EDGE_CANDIDATES if os.path.exists(p)), None)
        if not exe:
            raise RuntimeError("未找到 Edge/Chrome 可执行文件")
        self.exe = exe
        self.port = port or _free_port()
        self.headless = headless
        self.timeout = timeout
        self.profile = profile or os.path.join(
            cache_dir(), "edge-profile", f"p{os.getpid()}")   # 按进程隔离，避免多实例抢占
        os.makedirs(self.profile, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self.ws: RawWebSocket | None = None
        self._id = 0
        self._sessions: dict[str, str] = {}
        self._start()

    # ---------- 进程与连接 ----------
    def _start(self) -> None:
        args = [self.exe,
                f"--remote-debugging-port={self.port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-gpu", "--disable-features=Translate,OptimizationHints",
                # 无感化：不弹窗、不抢焦点、不进任务栏、不后台联网、不提示默认浏览器
                "--no-startup-window", "--disable-background-networking",
                "--disable-sync", "--disable-extensions", "--disable-component-update",
                "--disable-client-side-phishing-detection", "--no-service-autorun",
                "--password-store=basic", "--use-mock-keychain",
                "--window-position=-3000,-3000",
                "--window-size=1600,1400", "about:blank"]
        if self.headless:
            args.insert(1, "--headless=new")
        popen_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            # 不弹出控制台窗口、不抢前台焦点
            popen_kwargs["creationflags"] = 0x08000000 | 0x00000200   # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            popen_kwargs["startupinfo"] = subprocess.STARTUPINFO()
            try:
                popen_kwargs["startupinfo"].dwFlags |= subprocess.STARTF_USESHOWWINDOW
                popen_kwargs["startupinfo"].wShowWindow = 0          # SW_HIDE
            except Exception:
                pass
        self.proc = subprocess.Popen(args, **popen_kwargs)
        version = None
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=3) as r:
                    version = json.loads(r.read().decode())
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"浏览器进程退出，返回码 {self.proc.returncode}")
                time.sleep(0.5)
        if not version:
            raise RuntimeError("DevTools 端点未就绪")
        self.version = version
        self.ws = RawWebSocket(version["webSocketDebuggerUrl"], timeout=self.timeout)

    def close(self) -> None:
        if self.ws:
            self.ws.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- CDP ----------
    def send(self, method: str, params: dict | None = None,
             session_id: str | None = None, timeout: float | None = None) -> dict:
        assert self.ws
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            raw = self.ws.recv_text()
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("id") != mid:
                continue
            if "error" in obj:
                raise WSError(f"{method} 失败: {obj['error']}")
            return obj.get("result", {})
        raise WSError(f"{method} 超时")

    def new_page(self, url: str = "about:blank") -> str:
        """新建标签页并返回 sessionId。"""
        res = self.send("Target.createTarget", {"url": url})
        target_id = res["targetId"]
        attached = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = attached["sessionId"]
        self._sessions[sid] = target_id
        return sid

    def navigate(self, sid: str, url: str, wait: float = 0.0) -> None:
        self.send("Page.enable", {}, sid)
        self.send("Page.navigate", {"url": url}, sid)
        if wait:
            time.sleep(wait)

    def eval(self, sid: str, expression: str, timeout: float | None = None):
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": True,
        }, sid, timeout=timeout)
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            desc = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise WSError(f"JS 异常: {str(desc)[:300]}")
        return (res.get("result") or {}).get("value")

    def wait_for(self, sid: str, expression: str, timeout: float = 30.0,
                 interval: float = 0.6) -> bool:
        """轮询等待表达式为真。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(sid, expression):
                    return True
            except WSError:
                pass
            time.sleep(interval)
        return False

    def dom(self, sid: str) -> str:
        return self.eval(sid, "document.documentElement.outerHTML") or ""

    def text(self, sid: str) -> str:
        return self.eval(sid, "document.body ? document.body.innerText : ''") or ""

    def cookies(self, sid: str | None = None) -> list[dict]:
        res = self.send("Network.getCookies", {}, sid) if sid else self.send("Storage.getCookies", {})
        return res.get("cookies", [])

    def set_cookies(self, cookies: list[dict]) -> None:
        self.send("Network.setCookies", {"cookies": cookies})


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with Browser() as b:
        print("浏览器:", b.version.get("Browser"), "| 端口", b.port)
        sid = b.new_page()
        b.navigate(sid, "https://www.12306.cn/index/", wait=4)
        print("标题:", b.eval(sid, "document.title"))
        print("文本长度:", len(b.text(sid)))
