# windows-WPS-ops

> 在 Windows 沙箱环境下执行桌面软件诊断、下载、安装、提权、进程与注册表核查时的 Agent 操作规范与避坑清单。

**What is this?** An [Agent Skill](https://docs.claude.com/en/docs/claude-code/skills) that documents a set of non-obvious constraints in a sandboxed Windows agent environment, plus a fully worked case study: diagnosing and repairing WPS Office's "功能模块异常 / 点击即提示重新加载" failure without blindly reinstalling.

---

## 为什么需要这份规范

在沙箱化的 Windows 环境里跑 Agent，会反复遇到一类**假象**：

| 假象 | 真实原因 |
|---|---|
| 命令返回 exit code 0，但什么都没发生 | 工具输出被吞掉，或进程树被沙箱回收 |
| 注册表查出来的版本和磁盘上的对不上 | 32 位进程读的是 `WOW6432Node` 视图 |
| 安装程序界面一闪即没 | 前台命令结束会回收其拉起的进程树 |
| `start` 报「Windows 找不到文件」 | Git Bash 把 `/wait` 改写成了本地路径 |
| 提权安装卡了 2 分钟后报「拒绝访问」 | UAC 等了 120 秒自动超时，**等同于拒绝** |
| `curl -o` 下载返回成功，文件却不存在 | 沙箱静默拦下了写文件，需改用 Python `urllib` |
| 脚本退出码非 0，且毫无输出 | stdout 随进程被回收，关键结果必须落盘再读 |

不识别这些假象，就会顺着错误线索排查，最后得出「程序坏了要重装」的结论——而真实原因往往只是一个指向了已删除目录的注册表键。

## 内容结构

```
.
├── SKILL.md                          # 主规范：六条硬规则 + 标准动作 + 汇报要求
├── references/
│   └── wps-office-repair.md          # 案例：WPS Office「功能模块异常」完整修复过程
└── scripts/
    └── parallel_download.py          # 分片并发下载 + SHA256 校验
```

## 七条硬规则（摘要）

1. **读注册表用 Python `winreg`** —— 本环境中 `reg.exe` 被程序黑名单拦截，且不可绕过。
2. **不要依赖 PowerShell 工具的输出** —— 多次返回 exit code 0 但 stdout 为空；改用子进程或直接检查副作用。
3. **执行下载来的 exe 必须经 cmd 中转** —— bash 直接 exec 会报 `Permission denied`：
   ```
   cmd //c start //wait "" "C:\path\app.exe" [参数]
   ```
4. **Git Bash 会把 `/xxx` 开关改写成路径** —— 斜杠开关一律写双斜杠：`//c`、`//wait`、`//S`。以 `-` 开头的参数不受影响。
5. **GUI 程序与长任务必须后台运行** —— 否则命令返回时界面立刻消失。
6. **提权只能由用户完成** —— UAC 弹窗等待 120 秒后自动超时＝拒绝。启动后必须立刻提醒用户点「是」，不要等。
7. **命令正文里的敏感字样会整条被拦** —— 过滤器扫描的是整条命令字符串。长文本一律写临时文件再引用（如 `git commit -F <文件>`），不要让正文进入命令行。

完整内容与判定方法见 [`SKILL.md`](SKILL.md)。

## 除硬规则之外

`SKILL.md` 还收录以下几类容易踩空的地方：

- **出站请求** —— 不同域名的可达性并不一致（同一会话内 `api.github.com` 正常、`raw.githubusercontent.com` 超时），遇到超时要先区分「某域名不通」与「整体断网」，并优先改用稳定端点。
- **路径与临时文件** —— Git Bash 的 `/tmp` 与 Windows 原生程序互不认账，一律使用 Windows 原生临时路径。
- **输出捕获** —— 长脚本的 stdout 可能随进程回收而整体消失，关键结果落盘再读。
- **删除与权限** —— 应用自带的清理命令**自身往往不带提权**，在 ACL 受限目录上必然失败；而且它打印的「已全部清理」可能是**假信号**。判定该用「写探针」而不是直接删。
- **文件未被占用 ≠ 可删除** —— 占用与权限是两个独立问题，必须分开验证。

## 案例亮点

[`references/wps-office-repair.md`](references/wps-office-repair.md) 记录了一次真实修复，包含若干反直觉发现：

- 诊断工具建议重装，但**程序文件完好**——真因是注册表里一个指向已删除目录的幽灵键。
- WPS 自带的 `ksomisc.exe -clearOldVersions` **不带提权**，在 ACL 受限的旧版本目录上必然失败；而且它随后会打印一句 **`all old versions cleared`——这是假信号**，实际等于永久放弃重试。日志说 "cleared" 不能代替目录列举。
- **文件「未被占用」≠「可删除」**——占用与权限是两个独立问题，必须分开验证，否则会反复重试同一条走不通的路。
- 同机两个旧版本目录 ACL 可能不同，所以「有的能删有的删不掉」，不能一概而论。

## 使用方式

**用户级安装**（跨项目可用）：

```bash
git clone https://github.com/ztc1522021381/windows-WPS-ops.git
cp -r windows-WPS-ops ~/.workbuddy/skills/windows-agent-ops
```

**项目级安装**（随项目共享）：

```bash
git clone https://github.com/ztc1522021381/windows-WPS-ops.git
cp -r windows-WPS-ops <你的项目>/.workbuddy/skills/windows-agent-ops
```

> 技能目录名需为 `windows-agent-ops`，与 `SKILL.md` 中的 `name` 字段一致。

## 适用范围与免责声明

**适用范围**：这份规范记录的是**特定环境**（Windows 11 + 沙箱化 Agent 运行时）的实测观察。其中部分限制来自沙箱安全策略而非 Windows 本身，在其他环境中**未必成立**。

**请把它当作排查线索而非绝对真理**——每条规则都附了判定方法，建议先验证再套用。

案例部分涉及软件内部日志路径与命令行参数的观察，仅用于帮助用户避免不必要的重装；未涉及任何逆向工程或破解。文中提及的商标归各自所有者。

## License

[MIT](LICENSE)
