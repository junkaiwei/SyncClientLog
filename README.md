# IKUN LogCatch

Windows 桌面 Android 日志工具，支持模拟器和已开启 ADB 调试的真机。

## 运行

项目使用 Python 3.14、wxPython 和 uv：

```powershell
uv sync
uv run python ikunLogCatch.py
```

也可直接运行打包后的 `ikunLogCatch.exe`。配置和日志保存在启动时的工作目录。

## 使用

- **ADB 路径**：默认使用环境变量中的 `adb`；`emulated.txt` 可配置多个模拟器 ADB 路径，每行一个，`#` 开头为注释。
- **目标设备**：刷新设备列表，或通过“主动连接”输入 `IP/主机名[:端口]`；默认端口 5555。设备必须处于 `device` 状态。
- **包名**：`history/packages.txt` 每行填写 `别名 包名` 或仅包名；也可从设备的第三方应用列表添加。
- **开始抓取**：先检查应用 PID，再清空 Logcat 并抓取新日志。有包名时按 PID 过滤；清空包名可抓取全部 Logcat。应用重启后需要重新开始抓取。
- **查看文件**：浏览应用 `Android/data/<包名>` 下深度不超过 4 的文件，首次读取末尾 1000 行，随后点击“刷新”更新。展示的是快照，不自动刷新。UTF-8 / GBK 选项控制读取编码，失败时保留上次内容。
- **搜索**：默认全词匹配，可切换正则，使用上下按钮定位。全小写搜索忽略大小写；包含大写字符时区分大小写。最多保存 1000 个定位结果。
- **自动滚动**：开关决定新日志是否跟随末尾，可手动勾选或取消。向上滚动关闭开关（滚轮未实际移动视口也生效）；向下滚动且末行已展示时开启，尚未展示末行则保持开关原状态。程序追加日志和恢复视口不改变开关；搜索跳转仍会暂停跟随。显示超过 50 万字符时按完整行优先移除较旧内容，导出仍包含完整抓取会话。

- **导出 ADB 日志**：导出当前会话；文件查看模式导出最近一次成功读取的快照。
- **导出应用日志**：拉取 `/storage/emulated/0/Android/data/<包名>/files/outLog.txt`，保存在 `logs/<包名>/`。

临时会话文件为 `logs/session.tmp.log`，退出时删除。需要保留的内容应先导出。设备文件读取权限和 `find` / `tail` 命令支持由设备系统决定。

## 版本与打包

窗口版本只读取 `pyproject.toml` 中的 `project.version`。本地修改该字段后运行 `uv lock` 同步锁文件。

```powershell
uv run --with pyinstaller python -m PyInstaller --noconfirm ikunLogCatch.spec
```

产物为 `dist/ikunLogCatch.exe`，内置项目元数据，独立运行不依赖旁边的 TOML 文件。

GitHub Release 工作流通过 `RELEASE_TAG` 将标签传入 `scripts/prepare_version.py`，支持 `3.1.0`、`v3.1.0`、`v3.2.0rc1` 等规范格式。脚本验证标签并更新构建工作区的项目版本，再用相同 spec 打包；不提交版本更新。源码默认版本与发布版本可以不同，发布窗口显示以该次构建的标签为准。

## 开发与验证

```powershell
uv run python -m unittest discover -s tests -v
uv run python scripts/verify_build.py dist/ikunLogCatch.exe
```

测试覆盖命令失败判断、编码、版本准备、日志批量刷新、手动文件刷新、会话导出和实际 Windows wx 控件。打包检查把 EXE 复制到独立临时目录，验证窗口版本并正常关闭。真实设备的连接、PID 过滤和文件访问仍需设备验收。

`ikunLogCatch.py` 是当前入口；`logcatch` 包按配置、ADB、后台读取、会话文件、搜索、对话框、主窗口和版本读取划分职责。`history/sysclog.py` 为历史实现，不作为运行或打包入口。

日志显示采用 wxPython 自带的 `wx.stc.StyledTextCtrl`，由 `logcatch/logview.py` 管理 UTF-8 字节位置、搜索指示器及阅读锚点。字符上限仍按 Unicode 字符计算；ADB 抓取、会话保存及导出不依赖显示控件。
