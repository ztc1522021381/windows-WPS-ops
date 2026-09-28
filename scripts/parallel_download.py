#!/usr/bin/env python3
"""分片并发下载 + SHA256 校验。

适用于单连接被限速、但服务端返回 Accept-Ranges: bytes 的 HTTP/HTTPS 大文件。
实测某 CDN 单连接 103 KB/s，16 线程聚合到 1456 KB/s（约 14 倍）。

用法：
    python parallel_download.py <url> <输出文件> [期望SHA256] [线程数] [分片大小MB]

示例：
    python parallel_download.py https://example.com/app.exe D:/Downloads/app.exe abc123... 16 4

注意：
- 全部分片先缓存在内存，峰值内存约等于文件大小；超大文件请调大分片数不大幅降低线程数，
  或自行改为分片落盘（Range 直写文件 + seek）。
- 并发并非越高越好，超过约 16 线程后通常收益递减，且可能被服务端限流。
"""
import hashlib
import os
import queue
import sys
import threading
import time
import urllib.request


def head_size(url, timeout=30):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.headers.get("Accept-Ranges", "").lower() != "bytes":
            raise SystemExit("服务端不支持 Range，无法分片下载（Accept-Ranges 缺失）")
        return int(r.headers["Content-Length"])


def fetch(url, rng, buf, idx, retry=6):
    start, end = rng
    last = None
    for attempt in range(retry):
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=%d-%d" % (start, end)})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) != end - start + 1:
                raise IOError("短读 %d != %d" % (len(data), end - start + 1))
            buf[idx] = data
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

    buffers = [None] * len(ranges)
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
            fetch(url, ranges[i], buffers, i)
            done[0] += 1
            if done[0] % 5 == 0 or done[0] == len(ranges):
                print("进度 %d/%d" % (done[0], len(ranges)), flush=True)

    t0 = time.time()
    ths = [threading.Thread(target=worker) for _ in range(threads)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    dt = time.time() - t0

    with open(out, "wb") as f:
        for b in buffers:
            f.write(b)
    size = os.path.getsize(out)
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
