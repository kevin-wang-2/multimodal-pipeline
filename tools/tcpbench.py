#!/usr/bin/env python3
"""tcpbench — 零依赖的原生 TCP 吞吐测量（不经 SSH/加密层）。

协议: 客户端连接后发 16 字节头 = mode(1) + 7 字节填充 + 字节数(8, big-endian)
      'U' 客户端 -> 服务端     'D' 服务端 -> 客户端

用法:
  # 在 A 机上常驻服务端
  python3 tcpbench.py serve [--bind 0.0.0.0] [--port 9901]

  # 在 B 机上发起测量（B 主动连接，不受 A 侧防火墙影响）
  python3 tcpbench.py client <A的IP> [--port 9901] [--mb 200]
                             [--dir up|down|both] [--parallel 1] [--repeat 1]

说明: 服务端多线程，支持 --parallel 并发。两端各跑一遍即可覆盖双向。
"""
import argparse, socket, struct, sys, threading, time

HDR = struct.Struct("!c7xq")
CHUNK = 1 << 20


def _tune(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 1 << 20)
        except OSError:
            pass


def _send(sock, total):
    buf = b"\0" * CHUNK
    sent = 0
    while sent < total:
        n = sock.send(buf[: min(CHUNK, total - sent)])
        if n <= 0:
            break
        sent += n
    return sent


def _recv(sock, total):
    got = 0
    while got < total:
        d = sock.recv(min(CHUNK, total - got))
        if not d:
            break
        got += len(d)
    return got


def _handle(conn):
    try:
        _tune(conn)
        hdr = b""
        while len(hdr) < HDR.size:
            d = conn.recv(HDR.size - len(hdr))
            if not d:
                return
            hdr += d
        mode, total = HDR.unpack(hdr)
        if mode == b"U":
            _recv(conn, total)
        else:
            _send(conn, total)
    except OSError:
        pass
    finally:
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        conn.close()


def serve(bind, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind, port))
    s.listen(64)
    print(f"listening on {bind}:{port}", flush=True)
    while True:
        conn, peer = s.accept()
        _tune(conn)
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def _one(host, port, nbytes, mode):
    s = socket.create_connection((host, port), timeout=30)
    _tune(s)
    s.sendall(HDR.pack(mode, nbytes))
    t0 = time.perf_counter()
    moved = _send(s, nbytes) if mode == b"U" else _recv(s, nbytes)
    dt = time.perf_counter() - t0
    s.close()
    return dt, moved


def client(host, port, mb, direction, parallel, repeat):
    total = mb * 1024 * 1024
    per = total // parallel
    plans = [("上传", b"U", "client -> server"), ("下载", b"D", "server -> client")]
    if direction == "up":
        plans = plans[:1]
    elif direction == "down":
        plans = plans[1:]

    print(f"目标 {host}:{port}   每次 {mb} MiB   并发 {parallel}   重复 {repeat}")
    print(f"{'方向':<4} {'并发':>4} {'用时':>9} {'数据':>8} {'速率':>22}")
    for label, mode, _ in plans:
        for _rep in range(repeat):
            t0 = time.perf_counter()
            errs = []

            def worker():
                try:
                    _one(host, port, per, mode)
                except Exception as e:  # noqa: BLE001
                    errs.append(repr(e))

            ts = [threading.Thread(target=worker) for _ in range(parallel)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            dt = time.perf_counter() - t0
            mbps = (total / 1048576) / dt
            print(f"{label:<4} {parallel:>4} {dt:>8.2f}s {mb:>6}MiB "
                  f"{mbps:>9.1f} MB/s ({mbps*8:>6.0f} Mbit/s)"
                  + (f"   errors={errs}" if errs else ""))


def main():
    ap = argparse.ArgumentParser(add_help=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--bind", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=9901)
    cp = sub.add_parser("client")
    cp.add_argument("host")
    cp.add_argument("--port", type=int, default=9901)
    cp.add_argument("--mb", type=int, default=200)
    cp.add_argument("--dir", choices=["up", "down", "both"], default="both")
    cp.add_argument("--parallel", type=int, default=1)
    cp.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()
    if a.cmd == "serve":
        try:
            serve(a.bind, a.port)
        except KeyboardInterrupt:
            pass
    else:
        client(a.host, a.port, a.mb, a.dir, a.parallel, a.repeat)


if __name__ == "__main__":
    main()
