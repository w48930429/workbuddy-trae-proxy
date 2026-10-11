"""Qoder 账号管理小服务 —— OAuth 登录 + 一键签到（独立端口，可先行验证）。

不依赖、不修改正在运行的 wb_proxy（28086/28087）。验证通过后再并入主网关。

启动：python qoder_login_server.py  （默认 http://127.0.0.1:28090）
"""

from __future__ import annotations

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qoder_accounts as accounts
import qoder_checkin as checkin
import qoder_client as client

PORT = int(os.environ.get("QODER_LOGIN_PORT", "28091"))
HOST = "127.0.0.1"
PROXY = ""

DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qoder_dashboard.html")
BOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qoder_board.html")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默
        pass

    def _send(self, code: int, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except ValueError:
            return {}

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            try:
                with open(DASHBOARD_HTML, "rb") as fh:
                    body = fh.read()
            except Exception as exc:
                return self._send(500, {"error": f"dashboard.html unavailable: {exc}"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/board":
            try:
                with open(BOARD_HTML, "rb") as fh:
                    body = fh.read()
            except Exception as exc:
                return self._send(500, {"error": f"board.html unavailable: {exc}"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/accounts":
            return self._send(200, {"accounts": [a.__dict__ for a in accounts.list_accounts()]})
        if u.path == "/api/oauth/poll":
            login_id = (q.get("login_id") or [""])[0]
            status, acct = accounts.poll_login(login_id, PROXY)
            return self._send(200, {"status": status, "account": acct.__dict__ if acct else None})
        if u.path == "/api/quota":
            account_id = (q.get("account_id") or [""])[0]
            acct = accounts.get_account(account_id)
            if acct is None:
                return self._send(404, {"error": "account not found"})
            secret = accounts.get_secret(acct.id) or ""
            dt = checkin._device_token_of(secret)
            try:
                qi = accounts.fetch_quota(dt, acct.region, PROXY)
                remaining = 0.0
                if qi.addon_quota and qi.addon_quota.remaining > 0:
                    remaining = qi.addon_quota.remaining
                elif qi.user_quota:
                    remaining = qi.user_quota.remaining
                data = {
                    "plan": qi.plan,
                    "is_quota_exceeded": qi.is_quota_exceeded,
                    "expires_at": qi.expires_at,
                    "user_quota": qi.user_quota.__dict__ if qi.user_quota else None,
                    "addon_quota": qi.addon_quota.__dict__ if qi.addon_quota else None,
                    "remaining": remaining,
                }
                return self._send(200, data)
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        if u.path == "/api/oauth/start":
            body = self._read_json()
            sess = accounts.start_login(body.get("region") or "global")
            return self._send(200, {"login_id": sess.login_id, "login_url": sess.login_url})
        if u.path == "/api/checkin":
            body = self._read_json()
            aid = body.get("account_id")
            results = ([checkin.checkin_one(aid, PROXY)] if aid
                       else checkin.checkin_all(PROXY))
            return self._send(200, {"results": [r.__dict__ for r in results]})
        return self._send(404, {"error": "not found"})


def main():
    # 首次启动确保本机盐存在
    accounts.ensure_machine_salt()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Qoder 账号管理已启动：{url}")
    print(f"数据目录：{accounts.data_root()}")
    try:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    except Exception:  # noqa: BLE001
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已停止")


if __name__ == "__main__":
    main()