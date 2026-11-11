"""开发期：用浏览器调界面。静态文件从 ui/ 出，/api/<方法> 以 POST JSON 数组为参数调 Api。
.venv\\Scripts\\python.exe src\\app\\devserver.py [端口=8765]
"""

import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from api import Api  # noqa: E402

API = Api()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE / "ui"), **kw)

    def do_GET(self):
        # 开发模式标记：界面据此知道没有 pywebview，直接走 fetch
        if self.path in ("/", "/index.html"):
            body = (HERE / "ui" / "index.html").read_bytes().replace(b"<html", b'<html data-dev="1"', 1)
            # 样式/脚本带上修改时间：改完刷新就是新的（2026-11-09 改了图框样式，浏览器还用缓存里的旧 app.css，滑条还在）
            for f in ("app.css", "app.js"):
                v = str(int((HERE / "ui" / f).stat().st_mtime)).encode()
                body = body.replace(f'"{f}"'.encode(), f'"{f}?v='.encode() + v + b'"', 1)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self.send_error(404)
            return
        name = self.path[5:]
        n = int(self.headers.get("Content-Length") or 0)
        args = json.loads(self.rfile.read(n) or b"[]")
        fn = getattr(API, name, None)
        if fn is None or name.startswith("_"):
            self.send_error(404, "no such api")
            return
        try:
            body = json.dumps(fn(*args), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except Exception as e:  # 界面上看得到错误
            body = f"{type(e).__name__}: {e}".encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if "/api/job_status" in (str(args[0]) if args else ""):
            return
        super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"http://localhost:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
