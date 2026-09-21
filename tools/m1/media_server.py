"""模拟公网宿主签发的媒体端点（M1 验收用，跑在枢纽机上，nginx 临时把 /_media/ 反代到 127.0.0.1:8790）：/_media/<token>/in/<f> 只读 GET，/_media/<token>/out/<f> 只收 PUT。token 不对 → 403。"""
import os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.expanduser("~/mmp/media"); TOKEN = sys.argv[1]
class H(BaseHTTPRequestHandler):
    def _path(self, kind):
        parts = self.path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "_media" or parts[1] != TOKEN or parts[2] != kind or "/" in parts[3] or ".." in parts[3]:
            self.send_response(403); self.end_headers(); return None
        return os.path.join(ROOT, TOKEN, kind, parts[3])
    def do_GET(self):
        p = self._path("in")
        if p is None: return
        if not os.path.exists(p): self.send_response(404); self.end_headers(); return
        data = open(p, "rb").read(); self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_PUT(self):
        p = self._path("out")
        if p is None: return
        n = int(self.headers.get("Content-Length", "0")); open(p, "wb").write(self.rfile.read(n)); self.send_response(201); self.end_headers()
    def log_message(self, fmt, *a): sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))
ThreadingHTTPServer(("127.0.0.1", 8790), H).serve_forever()
