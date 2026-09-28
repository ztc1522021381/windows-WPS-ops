#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读诊断：列出 Windows「重启后删除/重命名」登记清单，并统计当前仍实际占地的项。

用途：当某文件"提权也删不掉、且未被占用"时，用它确认是否被登记进
      PendingFileRenameOperations（pending-delete 态）——这类文件只能等重启，
      或先清除登记再删。

用法：
    python check_pending_delete.py            # 人类可读报告
    python check_pending_delete.py --json     # 结构化输出
    python check_pending_delete.py --grep wps # 只看路径含指定关键字的项

只读，不修改注册表、不删除任何文件。
"""
import argparse
import json
import os
import sys

try:
    import winreg
except ImportError:
    print('本脚本仅在 Windows 上可用（需要 winreg）', file=sys.stderr)
    sys.exit(2)

SESSION_MANAGER = r'SYSTEM\CurrentControlSet\Control\Session Manager'
VALUE_NAME = 'PendingFileRenameOperations'


def strip_prefix(raw):
    """去掉 '*1' / '*2' 标记与 NT 对象前缀 '\\??\\'，还原为可用 os.path 访问的路径。

    关键坑：注册表里存的是 '\\??\\C:\\...' 形式的 NT 路径，直接交给
    os.path.exists() 会一律返回 False，造成"目标不存在"的全面误报。
    """
    p = raw
    if p[:2] in ('*1', '*2'):
        p = p[2:]
    while p.startswith('\\??\\'):
        p = p[4:]
    return p


def path_size(p):
    """返回路径占用字节数；路径不存在返回 None。"""
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return None
    if os.path.isdir(p):
        total = 0
        for root, _, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    return None


def read_entries():
    """读取并配对成操作组。返回 [{action, raw, path, size}]"""
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SESSION_MANAGER)
    except OSError as e:
        print('无法打开注册表键：%r' % (e,), file=sys.stderr)
        return []
    try:
        values, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return []
    finally:
        winreg.CloseKey(key)

    if len(values) % 2 != 0:
        print('警告：条目数为奇数（%d），注册表值可能已损坏' % len(values), file=sys.stderr)

    entries = []
    for i in range(0, len(values) - 1, 2):
        raw_src, raw_dst = values[i], values[i + 1]
        is_delete = raw_src.startswith('*1')
        path = strip_prefix(raw_src)
        entries.append({
            'action': 'delete' if is_delete else 'rename',
            'raw': raw_src,
            'path': path,
            'target': raw_dst if not is_delete else '',
            'size': path_size(path),
        })
    return entries


def dedupe_total(entries):
    """去重求和：父目录已计入时，跳过其子项，避免重复计算占用。"""
    live = [e for e in entries if e['size'] is not None and e['action'] == 'delete']
    live.sort(key=lambda e: len(e['path']))
    kept, seen_dirs = [], []
    for e in live:
        parent_counted = any(e['path'].lower().startswith(d.lower() + os.sep)
                             for d in seen_dirs)
        if parent_counted:
            continue
        kept.append(e)
        if os.path.isdir(e['path']):
            seen_dirs.append(e['path'])
    return kept, sum(e['size'] for e in kept)


def main():
    ap = argparse.ArgumentParser(description='列出 Windows 重启后删除/重命名登记清单（只读）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--grep', default='', help='仅显示路径含此关键字的项')
    args = ap.parse_args()

    entries = read_entries()
    if args.grep:
        entries = [e for e in entries if args.grep.lower() in e['path'].lower()]

    live = [e for e in entries if e['size'] is not None]
    gone = len(entries) - len(live)
    kept, total = dedupe_total(entries)

    if args.json:
        print(json.dumps({
            'value_exists': bool(entries),
            'groups': len(entries),
            'still_present': len(live),
            'already_gone': gone,
            'reclaimable_bytes': total,
            'reclaimable_mb': round(total / 1048576, 2),
            'entries': entries,
        }, ensure_ascii=False, indent=2))
        return 0

    if not entries:
        print('未找到 %s —— 当前没有待重启处理的文件。' % VALUE_NAME)
        return 0

    print('注册表值：HKLM\\%s\\%s' % (SESSION_MANAGER, VALUE_NAME))
    print('操作组 %d 个 ｜ 目标仍存在 %d 个 ｜ 目标已不在 %d 个'
          % (len(entries), len(live), gone))
    print('重启后实际可释放（已去重）：%.2f MB' % (total / 1048576))
    print()
    if kept:
        print('==== 重启后会清掉、且当前仍占地 ====')
        for e in sorted(kept, key=lambda x: -x['size']):
            label = '删除' if e['action'] == 'delete' else '重命名'
            print('  [%s] %9.2f MB  %s' % (label, e['size'] / 1048576, e['path']))
    noop = [e for e in entries if e['size'] is None]
    if noop:
        print()
        print('==== 已被处理过、重启时不会再有动作（%d 项，静默显示前 5 条）====' % len(noop))
        for e in noop[:5]:
            print('  [跳过] %s' % e['path'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
