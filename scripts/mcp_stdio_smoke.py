#!/usr/bin/env python3
"""MCP stdio 服务冒烟测试（只读，零业务副作用）。

用途
----
被 IDE / Agent 客户端拉起的 MCP 服务是 stdio 上的 JSON-RPC。这个脚本可以
自己把服务进程拉起来，走 handshake 并列出工具清单，用来回答：
  · 新编译的产物能不能正常启动？
  · 工具是否全部注册完整？
  · 服务端有没有在 stderr 上吐异常？

默认只做 `initialize` + `notifications/initialized` + `tools/list`，
不会触达任何业务逻辑。要调用具体工具必须显式传 --call，且**先自行
评估副作用**（凡是会动键盘 / 剪贴板 / 鼠标的工具，在其他自动化客户端
可能正在操作同一目标时，绝对不要贸然调用）。

用法
----
    python mcp_stdio_smoke.py "D:/path/to/Server.exe"
    python mcp_stdio_smoke.py "D:/path/to/Server.exe" --env DOTNET_ROOT=D:/dotnet-sdk/root
    python mcp_stdio_smoke.py "D:/path/to/Server.exe" --call read_st_code --args '{"pouNameHint":"__nonexistent__"}'

--call 只接受**幂等且无 UI 副作用**的工具；传不存在的名称来验证错误路径
是常用且安全的做法（能证明"明确报错"而不是"静默返回垃圾"）。
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = "2024-11-05"


class McpSmokeClient:
    def __init__(self, command, env_extra=None, cwd=None):
        env = dict(os.environ)
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd,
            env=env,
        )
        self.out_lines = []
        self.err_lines = []
        threading.Thread(
            target=lambda: [self.out_lines.append(l) for l in self.proc.stdout],
            daemon=True,
        ).start()
        threading.Thread(
            target=lambda: [self.err_lines.append(l) for l in self.proc.stderr],
            daemon=True,
        ).start()

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def wait_for_id(self, msg_id, timeout=30):
        """按 JSON-RPC id 匹配响应。不要按行号取——日志与通知会掺进 stdout。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in list(self.out_lines):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("id") == msg_id:
                    return obj
            time.sleep(0.2)
        return None

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def main():
    ap = argparse.ArgumentParser(description="MCP stdio 服务冒烟测试（默认零副作用）")
    ap.add_argument("command", nargs="+", help="启动服务的命令，例如 'D:/x/Server.exe'")
    ap.add_argument("--env", action="append", default=[],
                    help="附加环境变量 KEY=VALUE，可重复")
    ap.add_argument("--timeout", type=float, default=30, help="每次响应等待秒数")
    ap.add_argument("--call", help="可选：调用某个工具（请先评估副作用）")
    ap.add_argument("--args", default="{}", help="配合 --call 的工具参数 JSON")
    ap.add_argument("--show-stderr", type=int, default=0, help="展示 stderr 前 N 行")
    a = ap.parse_args()

    env_extra = {}
    for item in a.env:
        if "=" not in item:
            sys.exit("--env 需要 KEY=VALUE 形式: %s" % item)
        k, v = item.split("=", 1)
        env_extra[k] = v

    cwd = os.path.dirname(os.path.abspath(a.command[0])) or None
    cli = McpSmokeClient(a.command, env_extra, cwd)
    print("=== 启动服务进程 PID %d ===" % cli.proc.pid)
    print("    命令:", " ".join(a.command))

    ok = True

    cli.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "smoke-test", "version": "1.0"},
    }})
    r = cli.wait_for_id(1, a.timeout)
    if r and "result" in r:
        res = r["result"]
        print("\n[1] initialize OK")
        print("    serverInfo     :", json.dumps(res.get("serverInfo", {}), ensure_ascii=False))
        print("    protocolVersion:", res.get("protocolVersion"))
    else:
        ok = False
        print("\n[1] initialize 失败:", json.dumps(r, ensure_ascii=False) if r else "无响应（超时）")

    if ok:
        cli.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.3)

        cli.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        r = cli.wait_for_id(2, a.timeout)
        if r and "result" in r:
            tools = r["result"].get("tools", [])
            print("\n[2] tools/list OK -> 共 %d 个工具" % len(tools))
            for t in tools:
                desc = (t.get("description") or "").replace("\n", " ")[:64]
                print("    - %-32s %s" % (t.get("name"), desc))
        else:
            ok = False
            print("\n[2] tools/list 失败:", json.dumps(r, ensure_ascii=False) if r else "无响应（超时）")

    if a.call:
        print("\n[3] tools/call -> %s （注意：可能触及真实副作用）" % a.call)
        try:
            call_args = json.loads(a.args)
        except Exception as e:
            sys.exit("--args 不是合法 JSON: %s" % e)
        cli.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": a.call, "arguments": call_args}})
        r = cli.wait_for_id(3, max(a.timeout, 60))
        if r:
            res = r.get("result", {})
            for c in (res.get("content") or []):
                if c.get("type") == "text":
                    print(c["text"][:2000])
            print("    isError =", res.get("isError"))
            if r.get("error"):
                print("    JSON-RPC error:", json.dumps(r["error"], ensure_ascii=False))
        else:
            ok = False
            print("    无响应（超时）")

    cli.close()
    print("\n=== 测试进程已关闭 ===")

    if cli.err_lines and a.show_stderr:
        print("\n--- stderr 输出（前 %d 行）---" % a.show_stderr)
        for line in cli.err_lines[:a.show_stderr]:
            print("   ", line.rstrip())

    print("\n总体:", "通过" if ok else "存在问题")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
