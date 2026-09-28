# 案例：WPS Office「功能模块异常」修复

## 用户可见症状

- WPS 自带诊断工具报「检测到问题数：1，自动修复问题数：0」，项目为「WPS功能模块异常」，建议重新安装。
- 软件能打开，但**点几下操作就报错要求"重新加载"**，偶发「发送 WPS 错误报告」崩溃上报窗口。

## 根因模式（本机实测）

三类问题叠加，**程序文件本身完好**，无需按诊断工具建议直接重装：

1. **磁盘并存多个版本目录**：`C:\Program Files (x86)\Kingsoft Office Software\WPS Office\` 下同时存在 `12.1.0.19302`（旧）与 `12.1.0.21915`（在运行）。
2. **注册表版本账目互相矛盾**：
   - `HKLM\SOFTWARE\WOW6432Node\Kingsoft\Office\6.0\Common` 整套停留在极老的 `11.1.0.9995`，其 `InstallRoot` 指向**已不存在的目录** ← 诊断工具判定"模块异常"的直接依据。注意 32 位进程读的是 WOW6432Node 视图，与 64 位视图可能完全不同。
   - `HKCU\...\Common\updateversion` 与磁盘实际版本不一致。
   - 插件注册残留：`plugins\wpsbox\DllPath` 指向不存在的旧版本路径；`plugins\plgindex` 的 version 与 packedurl 指向旧版本插件包；`plugins\kdocercommon`、`plugins\kwpswin10toast`、`plugins\kwpsdlx` 同类。
3. **更新通道被服务端拒绝**：`%APPDATA%\Kingsoft\office6\log\update\wpsupdate_<date>.log` 中返回 `code="error" errorcode="10114" info="template mismatch"`，说明该机更新通道已失效，**无法靠"检查更新"自愈**。

## 诊断数据来源

| 数据 | 位置 |
|---|---|
| 安装根目录 | `HKCU\Software\Kingsoft\Office\6.0\Common\InstallRoot`（另有 HKLM 两个视图） |
| 卸载项 | `HKCU\...\CurrentVersion\Uninstall` 下 `WPS Office (版本号)` |
| 运行版本 | `HKCU\Software\Kingsoft\Office\6.0\{wpsoffice,wpscloudsvr}\Application Settings\version` |
| 诊断工具日志 | `%APPDATA%\Kingsoft\office6\log\ksomisc\ksomisc_<date>.log`（点"修复"会记录 `on_repairButton_clicked` → 启动 KdiagnosticTools） |
| 更新日志 | `%APPDATA%\Kingsoft\office6\log\update\wpsupdate_<date>.log` |
| 文档备份 | `%APPDATA%\Kingsoft\office6\backup\`；若曾自定义到 D 盘则为 `D:\Kingsoft\office6\backup\`（**是用户数据，勿删**） |

## 处置流程（已验证）

1. 只读诊断：确认磁盘版本目录、注册表三视图、运行版本。**先判断程序文件是否完好**（主程序数量与体积正常即完好）。
2. 取得官方安装包直链：
   ```
   winget show Kingsoft.WPSOffice.CN --source winget --accept-source-agreements --disable-interactivity
   ```
   实测直链形如 `https://official-package.wpscdn.cn/wps/download/WPS_Setup_<build>.exe`，清单同时给出 SHA256。用 `scripts/parallel_download.py` 并发下载并校验。
3. 关闭全部 WPS 相关进程。**先让用户 Ctrl+S 保存并关闭文档**，不要强杀有未保存内容的编辑进程；无窗口的常驻进程（诊断工具、云服务）可直接结束。
4. 以图形向导执行**覆盖安装**（不要用 `/S`，见下），UAC 必须由用户即时点击。
5. 安装后复查：版本目录（新版齐全、旧版被清空）、注册表三视图、磁盘释放量。
6. 启动新版，让用户实测原故障点。

## 关键坑

- **该安装包不支持静默安装**：`/S` 两次实测均返回退出码 0 却在磁盘与注册表上零变化。必须走图形向导。
- **覆盖安装不会刷新插件注册表残留**（`updateversion`、`plgindex`、`wpsbox.DllPath` 等仍指旧版本）。若故障复现，再清理这批 HKCU 残留（无需管理员，且有快照可回滚）；故障不复现则不要轻动。
- **WPS 自带旧版本清理命令**：`ksomisc.exe -clearOldVersions`。WPS 启动时自己也会调用它，日志会打印 `deleted old version dir: <版本>` 与 `delete failed, retry later: <版本>`（被占用文件会延后重试）。注意 `-clearOldVersions` 未出现在 `ksomisc.exe` 的字符串表中（动态拼接），不能靠字符串检索发现。
- **`-clearOldVersions` 不保证清干净，且它自己不带提权**（2026-09-28 实测）：日志固定打印
  `[isNeedAdmin]isCanRunElevated Failed and IsPersonalVersion, not needAdmin`
  —— 工具以**普通用户**身份运行。因此：
  - 若旧版本目录的 ACL 恰好允许当前用户写（该目录由用户态安装器较早创建时可能出现），清理会成功；
  - 若 ACL 只给 `Administrators` 写权限（覆盖安装后新布局常见），则**每次调用都必然**打印 `delete failed, retry later: <版本>`，并且随后会打印自相矛盾的 `all old versions cleared, reset NeedClearOldVersions` —— **这句是假信号，不等于真清干净了**，务必回磁盘复核。
  - 实测同一台机器上两个旧目录表现不同：`12.1.0.19302` 被成功删除，`12.1.0.21915` 三次调用均失败。**不要用"日志说 cleared"代替目录列举。**
- **判定残留是否真需要提权的可靠方法**：在目标目录里跑一次写探针。
  ```python
  import os, uuid, ctypes
  d = r'<残留目录>'
  try:
      t = os.path.join(d, 'wtest_%s.tmp' % uuid.uuid4().hex[:8]); open(t,'w').close(); os.remove(t)
      print('有写权限 -> 可直接删')
  except PermissionError:
      print('无写权限 -> 必须提权删除')
  print('当前进程管理员:', bool(ctypes.windll.shell32.IsUserAnAdmin()))
  ```
  比 `os.remove` 的报错更早、更干净地给出定性结论（`os.remove` 会被本机 safe-delete 拦截并抛出 trash-failed，掩盖真实原因）。
- **文件"未被占用"≠"可删除"**：本机实测 `qingnse64.dll`（15.3MB）已无任何进程加载（`CreateFileW` 独占打开成功），但 `os.remove` 仍被 safe-delete 拦下、`os.rename` 报 `WinError 5 拒绝访问`。**占用与权限是两个独立问题，必须分开验证**，否则会反复重试同一条走不通的路。
- 安装过程会解包到 `%TEMP%\nsc*.tmp`（NSIS 插件：`AccessControl.dll`/`System.dll`/`v6svc_oem.dll`）与 `%TEMP%\wps\~<id>\CONTROL\`（Qt 界面，`kpacketui.dll`），安装完成后应清理。
- 覆盖安装完成后根目录的 `wpsupdate.exe` 被移除，仅保留 `ksolaunch.exe`。

## 用户数据保护

覆盖安装保留 `%APPDATA%\Kingsoft\`、`%LOCALAPPDATA%\Kingsoft\` 与文档备份目录；不改动用户文档。卸载项与文件关联在安装后正确指向新版本。回滚手段：安装前 dump 的注册表分支 JSON + 还原脚本。
