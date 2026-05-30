#!/usr/bin/env python3
"""B站扫码登录获取 Cookie 工具"""

import argparse
import http.server
import json
import logging
import signal
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
import qrcode
from typing import Any, Optional

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
    DATA_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).parent
    DATA_DIR = BASE_DIR
COOKIE_FILE = BASE_DIR / "cookie.json"
HTML_FILE = DATA_DIR / "index.html"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com/",
}
BILIBILI_NAV_API = "https://api.bilibili.com/x/web-interface/nav"
BILIBILI_RELATION_STAT_API = "https://api.bilibili.com/x/relation/stat?vmid={}"
QR_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

logger = logging.getLogger(__name__)


# ============================================================
#  Thread-safe application state
# ============================================================

@dataclass
class AppState:
    cookie: str = ""
    login_status: str = "等待初始化"
    status_detail: str = "正在准备环境..."
    qrcode_url: str = ""
    expire_time: str = "无"
    expire_timestamp: int = 0
    user_info: Optional[dict] = None
    cookie_valid: Optional[bool] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.login_status,
                "detail": self.status_detail,
                "qrcode_url": self.qrcode_url,
                "expire": self.expire_time,
                "expire_ts": self.expire_timestamp,
                "has_cookie": bool(self.cookie),
                "user_info": self.user_info,
                "cookie_valid": self.cookie_valid,
            }

    def get_cookie(self) -> str:
        with self._lock:
            return self.cookie

    def set_cookie(self, value: str) -> None:
        with self._lock:
            self.cookie = value


state = AppState()


# ============================================================
#  File I/O
# ============================================================

def read_cookie_file() -> dict:
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cookie_file(cookie: str) -> None:
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "cookie": cookie,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("保存 cookie.json 失败: %s", e)


# ============================================================
#  Bilibili Login Logic
# ============================================================

class BilibiliLogin:
    def __init__(self, app_state: AppState) -> None:
        self.state = app_state
        self.qrcode_key = ""
        self.qrcode_url = ""
        self._poll_thread: Optional[threading.Thread] = None

    @staticmethod
    def _build_request(url: str) -> urllib.request.Request:
        req = urllib.request.Request(url)
        for k, v in DEFAULT_HEADERS.items():
            req.add_header(k, v)
        return req

    # ----- QR code generation -----

    def get_qrcode(self) -> tuple[bool, str]:
        try:
            req = self._build_request(QR_GENERATE_API)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data: dict = json.loads(resp.read().decode("utf-8"))

            if data.get("code") != 0:
                msg = data.get("message") or "接口返回异常"
                self.state.update(qrcode_url="", login_status="二维码获取失败", status_detail=msg)
                return False, msg

            self.qrcode_key = data["data"]["qrcode_key"]
            self.qrcode_url = data["data"]["url"]
            self.state.update(
                qrcode_url=self.qrcode_url,
                login_status="等待扫码",
                status_detail="请使用哔哩哔哩 APP 扫描二维码。",
            )
            return True, ""
        except Exception as e:
            self.state.update(qrcode_url="", login_status="二维码获取失败", status_detail=f"网络请求失败: {e}")
            return False, str(e)

    # ----- QR code polling (runs in thread) -----

    def check_login(self, poll_key: str) -> None:
        while poll_key == self.qrcode_key:
            try:
                params = urllib.parse.urlencode({"qrcode_key": poll_key})
                req = self._build_request(f"{QR_POLL_API}?{params}")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data: dict = json.loads(resp.read().decode("utf-8"))
                    cookie_headers = resp.headers.get_all("Set-Cookie", [])

                if poll_key != self.qrcode_key:
                    return

                if data.get("code") != 0:
                    self.state.update(login_status="登录状态获取失败", status_detail=data.get("message") or "请稍后重试")
                    time.sleep(2)
                    continue

                code = data.get("data", {}).get("code")

                if code == 0:
                    cookie_str = self._extract_cookies(cookie_headers)
                    if cookie_str:
                        self.state.set_cookie(cookie_str)
                        save_cookie_file(cookie_str)
                        exp, ts = self._parse_expiry(cookie_str)
                        self.state.update(expire_time=exp, expire_timestamp=ts)
                    self.state.update(
                        login_status="登录成功",
                        status_detail="Cookie 已保存到 cookie.json，可直接复制注入。",
                        qrcode_url="",
                    )
                    logger.info("新登录成功，Cookie 已保存")
                    return

                messages = {
                    86101: ("等待扫码", "请使用哔哩哔哩 APP 扫描二维码。"),
                    86069: ("已扫码，等待确认", "请在手机上确认登录。"),
                    86090: ("已扫码，等待确认", "请在手机上确认登录。"),
                    86038: ("二维码已过期", "点击「刷新二维码」重新获取。"),
                }
                if code in messages:
                    status, detail = messages[code]
                    self.state.update(login_status=status, status_detail=detail)
                    if code == 86038:
                        self.state.update(qrcode_url="")
                        return
                else:
                    self.state.update(login_status="登录状态更新中", status_detail=data.get("message") or f"状态码: {code}")

                time.sleep(1.5)
            except Exception as e:
                if poll_key == self.qrcode_key:
                    self.state.update(login_status="登录异常", status_detail=f"网络请求失败: {e}")
                time.sleep(2)

    # ----- Cookie utilities -----

    @staticmethod
    def _extract_cookies(cookie_headers: list[str]) -> str:
        if not cookie_headers:
            return ""
        if isinstance(cookie_headers, str):
            cookie_headers = [cookie_headers]
        cookies, seen = [], set()
        for header in cookie_headers:
            item = header.split(";", 1)[0].strip()
            if "=" not in item or item in seen:
                continue
            seen.add(item)
            cookies.append(item)
        return "; ".join(cookies)

    @staticmethod
    def normalize_cookie(cookie_str: str) -> str:
        if not cookie_str:
            return ""
        skip_keys = {"expires", "path", "domain", "max-age", "secure", "httponly", "samesite"}
        items = []
        for part in cookie_str.split(";"):
            item = part.strip()
            if not item or "=" not in item:
                continue
            key = item.split("=", 1)[0].strip().lower()
            if key in skip_keys:
                continue
            items.append(item)
        return "; ".join(items)

    @staticmethod
    def _parse_expiry(cookie_str: str) -> tuple[str, int]:
        try:
            for part in BilibiliLogin.normalize_cookie(cookie_str).split(";"):
                part = part.strip()
                if not part.startswith("SESSDATA="):
                    continue
                value = part.split("=", 1)[1]
                segments = urllib.parse.unquote(value).split(",")
                if len(segments) >= 2:
                    ts = int(segments[1].strip())
                    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"), ts
        except Exception:
            pass
        return "无法解析过期时间", 0

    @staticmethod
    def build_inject_code(cookie_str: str) -> str:
        cookie = BilibiliLogin.normalize_cookie(cookie_str)
        if not cookie:
            return ""
        lines = []
        for item in cookie.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            safe = item.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'document.cookie="{safe}; path=/; domain=.bilibili.com";')
        return "\n".join(lines)

    @staticmethod
    def build_curl_format(cookie_str: str) -> str:
        return f"Cookie: {BilibiliLogin.normalize_cookie(cookie_str)}"

    # ----- Cookie validation -----

    def test_cookie(self, cookie_str: str) -> dict:
        if not cookie_str:
            return {"valid": False, "message": "Cookie 为空"}
        try:
            req = self._build_request(BILIBILI_NAV_API)
            req.add_header("Cookie", cookie_str)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data: dict = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                info = data["data"]
                uid = info.get("mid", 0)
                follower, following = 0, 0
                try:
                    sreq = self._build_request(BILIBILI_RELATION_STAT_API.format(uid))
                    sreq.add_header("Cookie", cookie_str)
                    with urllib.request.urlopen(sreq, timeout=10) as sresp:
                        sdata: dict = json.loads(sresp.read().decode("utf-8"))
                    if sdata.get("code") == 0:
                        sinfo = sdata.get("data", {})
                        follower = sinfo.get("follower", 0)
                        following = sinfo.get("following", 0)
                except Exception:
                    pass
                return {
                    "valid": True,
                    "uname": info.get("uname", ""),
                    "uid": uid,
                    "level": info.get("level_info", {}).get("current_level", 0),
                    "face": info.get("face", ""),
                    "follower": follower,
                    "following": following,
                    "message": f"已登录: {info.get('uname', 'unknown')}",
                }
            return {"valid": False, "message": "Cookie 已失效或未登录"}
        except Exception as e:
            return {"valid": False, "message": f"验证失败: {e}"}

    # ----- Lifecycle -----

    def load_local_cookie(self) -> str:
        if not COOKIE_FILE.exists():
            self.state.update(expire_time="无", expire_timestamp=0)
            return ""
        try:
            data = read_cookie_file()
            cookie = self.normalize_cookie(data.get("cookie", ""))
            if cookie:
                exp, ts = self._parse_expiry(cookie)
                self.state.update(expire_time=exp, expire_timestamp=ts)
                if cookie != data.get("cookie", ""):
                    save_cookie_file(cookie)
                return cookie
        except Exception as e:
            logger.error("读取 cookie.json 失败: %s", e)
        self.state.update(expire_time="无", expire_timestamp=0)
        return ""

    def start_login_flow(self) -> tuple[bool, str]:
        success, msg = self.get_qrcode()
        if not success:
            return False, msg
        self._poll_thread = threading.Thread(
            target=self.check_login, args=(self.qrcode_key,), daemon=True
        )
        self._poll_thread.start()
        return True, ""


# ============================================================
#  HTTP Server
# ============================================================

class MyHandler(http.server.SimpleHTTPRequestHandler):
    login_obj: BilibiliLogin = None  # type: ignore[assignment]

    def send_response(self, code: int, message: str = None) -> None:
        super().send_response(code, message)
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


    def send_json(self, data: dict, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # ----- API routes -----

    def do_GET(self) -> None:
        if self.path == "/api/status":
            return self.send_json(state.snapshot())

        if self.path == "/api/cookie":
            cookie = state.get_cookie()
            data = read_cookie_file()
            if not cookie:
                cookie = data.get("cookie", "")
            if cookie:
                state.set_cookie(cookie)
            return self.send_json({
                "cookie": cookie,
                "inject_code": BilibiliLogin.build_inject_code(cookie),
                "curl_format": BilibiliLogin.build_curl_format(cookie),
            })

        if self.path == "/api/test":
            cookie = state.get_cookie()
            result = self.login_obj.test_cookie(cookie)
            state.update(cookie_valid=result["valid"], user_info=result if result["valid"] else None)
            return self.send_json(result)

        if self.path.startswith("/api/qrcode"):
            return self.serve_qrcode()

        if self.path.startswith("/api/avatar"):
            return self.proxy_avatar()

        if self.path == "/api/delete":
            state.update(cookie="", expire_time="无", expire_timestamp=0,
                         user_info=None, cookie_valid=None)
            try:
                if COOKIE_FILE.exists():
                    COOKIE_FILE.unlink()
            except Exception as e:
                return self.send_json({"ok": False, "message": f"删除失败: {e}"}, 500)
            return self.send_json({"ok": True, "message": "Cookie 已清除"})

        if self.path == "/":
            return self.serve_html()

        super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/refresh":
            success, msg = self.login_obj.start_login_flow()
            if success:
                self.send_json({"ok": True, "message": "二维码已刷新，请扫码。"})
            else:
                self.send_json({"ok": False, "message": f"刷新失败: {msg or '请检查网络'}"}, 500)
            return

        self.send_json({"ok": False, "message": "未知路由"}, 404)

    # ----- Local QR code generation -----

    def serve_qrcode(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        url = (params.get("url") or [None])[0]
        if not url:
            return self.send_json({"error": "missing url"}, 400)
        try:
            img = qrcode.make(url)
            buf = BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            logger.warning("二维码请求被客户端中断")
        except Exception as e:
            try:
                self.send_json({"error": str(e)}, 500)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

    # ----- Avatar proxy (bypass B站 CDN referrer check) -----

    def proxy_avatar(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        url = (params.get("url") or [None])[0]
        if not url:
            return self.send_json({"error": "missing url"}, 400)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            ctype = resp.headers.get("Content-Type", "image/webp")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            try:
                self.send_json({"error": str(e)}, 502)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

    # ----- HTML page -----

    def serve_html(self) -> None:
        try:
            html = HTML_FILE.read_text(encoding="utf-8")
        except Exception:
            html = "<h1>500 页面加载失败</h1><p>index.html 文件不存在</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True




# ============================================================
#  Entry point
# ============================================================

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _find_port(start: int = 8888, max_attempts: int = 10) -> int:
    for port in range(start, start + max_attempts):
        try:
            with ThreadedHTTPServer(("", port), MyHandler) as test:
                test.server_close()
            return port
        except OSError:
            continue
    raise RuntimeError(f"端口 {start}-{start + max_attempts - 1} 均被占用，请指定其他端口")


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(description="B站扫码登录获取 Cookie 工具")
    parser.add_argument("--port", type=int, default=8888, help="监听端口 (默认: 8888)")
    args = parser.parse_args()

    actual_port = _find_port(args.port)

    login_obj = BilibiliLogin(state)
    MyHandler.login_obj = login_obj

    cookie = login_obj.load_local_cookie()
    if cookie:
        logger.info("已加载本地 Cookie")
    else:
        logger.info("未检测到本地 Cookie")

    logger.info("正在获取登录二维码…")
    success, msg = login_obj.start_login_flow()
    if not success:
        logger.error("获取二维码失败: %s", msg)
        raise SystemExit(1)

    separator = "=" * 50
    httpd = ThreadedHTTPServer(("", actual_port), MyHandler)

    def shutdown_handler(signum: int, frame: object = None) -> None:
        logger.info("\n正在关闭服务...")
        httpd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logger.info("  访问地址 → http://127.0.0.1:%d", actual_port)
    logger.info(separator)
    httpd.serve_forever()


if __name__ == "__main__":
    banner = "=" * 50
    logger.info(banner)
    logger.info("   B站扫码登录获取 Cookie 工具")
    logger.info(banner)
    main()
