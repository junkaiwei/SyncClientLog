"""IKUN LogCatch desktop entrypoint."""
import os
import wx
from logcatch.config import ADB_PATHS_FILE, PACKAGES_FILE
from logcatch.frame import CatchLogFrame

def main():
    """初始化并运行程序"""
    # 确保配置文件存在
    if not os.path.exists(ADB_PATHS_FILE):
        with open(ADB_PATHS_FILE, "w", encoding="utf-8") as f:
            f.write("# 每行添加一个ADB可执行文件路径，例如: C:\\adb\\adb.exe\n")
    if not os.path.exists(PACKAGES_FILE):
        with open(PACKAGES_FILE, "w", encoding="utf-8") as f:
            f.write("# 格式: 别名 包名 (别名是可选的)\n# 例如: 主应用 com.example.app\n")

    app = wx.App(False)
    frame = CatchLogFrame()
    frame.Show()
    app.MainLoop()


if __name__ == '__main__':
    main()
