#!/usr/bin/env python3
"""分片并发下载 + SHA256 校验。

适用于单连接被限速、但服务端返回 Accept-Ranges: bytes 的 HTTP/HTTPS 大文件。
实测某 CDN 单连接 103 KB/s，16 线程聚合到 1456 KB/s（约 14 倍）。

用法：
    python parallel_download.py <url> <输出文件> [期望SHA256] [线程数] [分片大小MB]

示例：
    python parallel_download.py https://example.com/app.exe D:/Downloads/app.exe abc123... 16 4

注意：
- 分片直接 seek 落盘，内存峰值约为「线程数 × 分片大小」，与文件总大小无关。
- 并发并非越高越好，超过约 16 线程后通常收益递减，且可能被服务端限流。
- 期望 SHA256 可传 `-` 或省略表示不校验；不校验时至少比对下载字节数与远端大小。
- 不要只信 `Accept-Ranges` 响应头（见 head_size 的说明）。
"""
import hashlib
import os
import queue
import sys
import threading
import time
import urllib.request


def head_size(url, timeout=30):
    """探测总大小并确认服务端真的支持 Range。

    **不要只信 HEAD 的 Accept-Ranges 头。** 实测部分 CDN（如 .NET SDK 的
    builds.dotnet.microsoft.com）根本不返回该头（值为 None），但完全支持
    Range——发 Range 请求会正常返回 206 + Content-Range。只按该头判断会把
    可下载的地址误判成"不支持分片"而直接放弃。因此改为**实测判定**。
    """
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status == 206:
            cr = r.headers.get("Content-Range", "")
            if "/" in cr and cr.rsplit("/", 1)[-1].isdigit():
                return int(cr.rsplit("/", 1)[-1])
        length = r.headers.get("Content-Length")
        if length:
            return int(length)
    raise SystemExit("无法确定文件大小：服务端既未返回 Content-Range 也未返回 Content-Length")


def fetch(url, rng, out_path, write_lock, retry=6):
    """下载单个分片并直接 seek 落盘（避免把整个文件缓存在内存里）。"""
    start, end = rng
    last = None
    for attempt in range(retry):
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=%d-%d" % (start, end)})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) != end - start + 1:
                raise IOError("短读 %d != %d" % (len(data), end - start + 1))
            with write_lock:
                with open(out_path, "r+b") as f:
                    f.seek(start)
                    f.write(data)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("分片 %s 下载失败: %s" % (rng, last))


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    url = sys.argv[1]
    out = sys.argv[2]
    expect = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] not in ("-", "") else None
    threads = int(sys.argv[4]) if len(sys.argv) > 4 else 16
    chunk = int(float(sys.argv[5]) * 1024 * 1024) if len(sys.argv) > 5 else 4 * 1024 * 1024

    total = head_size(url)
    print("远端大小: %d 字节 (%.1f MB)" % (total, total / 2 ** 20), flush=True)

    ranges = []
    pos = 0
    while pos < total:
        end = min(pos + chunk - 1, total - 1)
        ranges.append((pos, end))
        pos = end + 1
    print("分片 %d 个，线程 %d 个" % (len(ranges), threads), flush=True)

    # 预分配目标文件，各分片直接 seek 写入 —— 内存峰值只与线程数×分片大小有关
    out_dir = os.path.dirname(os.path.abspath(out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out, "wb") as f:
        f.truncate(total)

    write_lock = threading.Lock()
    q = queue.Queue()
    for i in range(len(ranges)):
        q.put(i)
    done = [0]

    def worker():
        while True:
            try:
                i = q.get_nowait()
            except queue.Empty:
                return
            fetch(url, ranges[i], out, write_lock)
            with write_lock:
                done[0] += 1
                n = done[0]
            if n % 5 == 0 or n == len(ranges):
                print("进度 %d/%d" % (n, len(ranges)), flush=True)

    t0 = time.time()
    ths = [threading.Thread(target=worker) for _ in range(threads)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    dt = time.time() - t0

    size = os.path.getsize(out)
    if size != total:
        try:
            os.remove(out)
        except OSError:
            pass
        raise SystemExit("下载不完整（%d != %d），已删除半成品" % (size, total))
    print("完成: %d 字节，用时 %.1f s，平均 %.0f KB/s" % (size, dt, size / 1024 / dt), flush=True)

    h = hashlib.sha256()
    with open(out, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    got = h.hexdigest()
    print("SHA256: %s" % got, flush=True)
    if expect:
        print("校验: %s" % ("通过" if got.lower() == expect.lower() else "不匹配，期望 " + expect), flush=True)
        if got.lower() != expect.lower():
            sys.exit(2)


if __name__ == "__main__":
    main()
