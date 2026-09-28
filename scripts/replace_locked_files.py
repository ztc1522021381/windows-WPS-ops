#!/usr/bin/env python3
"""零中断替换被运行中进程占用的文件（Windows 重命名法）。

原理
----
Windows 允许**重命名**已被加载的 DLL / exe（文件句柄绑定的是文件对象，
重命名只改目录项；删除才会被拒）。因此不必先杀进程：

    旧文件 --rename--> 旧文件.old-<stamp>
    新文件 --copy----> 原路径

正在运行的进程继续执行内存中的旧代码、完全不受影响；客户端下次重启/重连
才加载新代码。旧文件仍留在磁盘上，随时可回滚。

先决条件
--------
必须**先按内容哈希比对**源目录与目标目录，只替换真正不同的文件。依赖包
通常逐字节一致——实测某 .NET 项目 41 个产物文件里只有 3 个需要替换。

用法
----
    # 1) 只看差异，不动任何文件
    python replace_locked_files.py --src D:/build/bin/Release/net10.0-windows \
                                   --dst D:/proj/bin/Release/net10.0-windows --dry-run

    # 2) 执行替换
    python replace_locked_files.py --src ... --dst ... --apply

    # 3) 回滚
    python replace_locked_files.py --rollback --dst ... --stamp 20260928
"""

import argparse
import hashlib
import os
import shutil
import sys


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(root):
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, root)] = full
    return out


def compare(src, dst):
    a, b = walk(src), walk(dst)
    only_new = sorted(set(a) - set(b))
    only_old = sorted(set(b) - set(a))
    changed, same = [], []
    for rel in sorted(set(a) & set(b)):
        if md5(a[rel]) == md5(b[rel]):
            same.append(rel)
        else:
            changed.append(rel)
    return a, b, changed, only_new, only_old, same


def do_replace(src, dst, stamp, targets=None, dry_run=True):
    a, b, changed, only_new, only_old, same = compare(src, dst)
    todo = [r for r in changed if targets is None or r in targets]

    print("源目录文件 %d 个，目标目录文件 %d 个" % (len(a), len(b)))
    print("内容一致 %d 个 | 内容不同 %d 个 | 仅源有 %d 个 | 仅目标有 %d 个"
          % (len(same), len(changed), len(only_new), len(only_old)))
    print("\n需要替换的文件：")
    for rel in changed:
        flag = "  <-- 本次处理" if rel in todo else "  (未选中，跳过)"
        print("   %-56s %9d -> %9d%s" % (rel, os.path.getsize(b[rel]), os.path.getsize(a[rel]), flag))
    if not changed:
        print("   （无差异，无需替换）")

    if dry_run:
        print("\n[dry-run] 未改动任何文件。确认无误后加 --apply 执行。")
        return 0

    done, failed = [], []
    for rel in todo:
        s, d = a[rel], b[rel]
        bak = d + ".old-" + stamp
        try:
            if os.path.exists(bak):
                os.remove(bak)
            os.rename(d, bak)
            print("[ok] 已改名保留: %s" % os.path.basename(bak))
        except Exception as e:
            print("[FAIL] 重命名失败 %s: %s %s" % (rel, type(e).__name__, e))
            failed.append(rel)
            continue
        try:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            print("     [ok] 新文件就位: %s  %d bytes" % (rel, os.path.getsize(d)))
            done.append(rel)
        except Exception as e:
            print("     [FAIL] 复制失败 %s: %s %s" % (rel, type(e).__name__, e))
            failed.append(rel)

    print("\n=== 替换后校验 ===")
    allok = True
    for rel in todo:
        ok = os.path.exists(b[rel]) and md5(a[rel]) == md5(b[rel])
        allok &= ok
        print("  %-56s %s" % (rel, "一致" if ok else "不一致 <<<"))
    _ = (only_new, only_old)

    print("\n完成 %d 个，失败 %d 个" % (len(done), len(failed)))
    print("旧文件备份后缀: .old-%s（回滚时改名回去即可）" % stamp)
    if failed:
        print("失败清单:", failed)
        print("提示：若失败原因是文件被独占锁定（非共享读取），需先停止占用进程。")
    return 0 if allok else 1


def do_rollback(dst, stamp):
    n = 0
    for dirpath, _dirnames, filenames in os.walk(dst):
        for name in filenames:
            if name.endswith(".old-" + stamp):
                bak = os.path.join(dirpath, name)
                original = bak[: -len(".old-" + stamp)]
                try:
                    if os.path.exists(original):
                        os.remove(original)
                    os.rename(bak, original)
                    print("[ok] 已回滚:", os.path.relpath(original, dst))
                    n += 1
                except Exception as e:
                    print("[FAIL] 回滚失败 %s: %s" % (name, e))
    print("\n共回滚 %d 个文件" % n)
    print("注意：若占用进程仍在运行，它内存里依旧是旧代码，需重启该进程才生效。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="零中断替换被运行中进程占用的文件（Windows 重命名法）")
    ap.add_argument("--src", help="新产物目录（来源）")
    ap.add_argument("--dst", required=True, help="目标部署目录")
    ap.add_argument("--stamp", default=None, help="备份后缀标记，默认 YYYYMMDD")
    ap.add_argument("--only", nargs="*", help="只处理指定相对路径，缺省处理全部差异")
    ap.add_argument("--dry-run", action="store_true", help="只列差异，不改动（默认）")
    ap.add_argument("--apply", action="store_true", help="实际执行替换")
    ap.add_argument("--rollback", action="store_true", help="从 .old-<stamp> 回滚")
    a = ap.parse_args()

    if a.stamp is None:
        import datetime
        a.stamp = datetime.datetime.now().strftime("%Y%m%d")

    if a.rollback:
        if not os.path.isdir(a.dst):
            sys.exit("目标目录不存在: %s" % a.dst)
        return do_rollback(a.dst, a.stamp)

    if not a.src:
        sys.exit("需要 --src（或使用 --rollback）")
    for p in (a.src, a.dst):
        if not os.path.isdir(p):
            sys.exit("目录不存在: %s" % p)

    return do_replace(a.src, a.dst, a.stamp, set(a.only) if a.only else None,
                      dry_run=not a.apply)


if __name__ == "__main__":
    sys.exit(main())
