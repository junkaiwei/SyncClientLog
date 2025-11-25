import wx
import os
import re
import subprocess
import threading
import time
import functools
import weakref
from collections import deque
import resources
import shutil

LOGS_DIR = "logs"
ADB_PATHS_FILE = "emulated.txt"
PACKAGES_FILE = "packages.txt"
SESSION_LOG_FILENAME = "session.tmp.log"

# 性能配置
MAX_CHARS = 500000
BUFFER_SIZE = 100
FLUSH_INTERVAL = 0.5  # 刷新间隔，单位秒
SEARCH_DELAY = 1000  # 搜索延迟，单位毫秒


class ConfigManager:
    """配置文件管理"""

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def parse_adb_paths(filename):
        if not os.path.exists(filename): return []
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip() and not line.startswith('#')]
        except Exception:
            return []

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def parse_packages(filename):
        if not os.path.exists(filename): return []
        result = []
        pattern = re.compile(r'^[a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+$')
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'): continue
                    parts = line.rsplit(None, 1)
                    if len(parts) == 2 and pattern.match(parts[1]):
                        result.append((parts[0], parts[1]))
                    else:
                        result.append((line, line))
        except Exception:
            pass
        return sorted(result, key=lambda x: x[0].lower())


class SearchResultManager:
    """搜索结果管理"""

    def __init__(self):
        self.results = deque(maxlen=1000)
        self.current_index = -1

    def clear(self):
        self.results.clear()
        self.current_index = -1

    def add_result(self, start, end):
        self.results.append((start, end))

    def get_next(self):
        if not self.results: return None
        self.current_index = (self.current_index + 1) % len(self.results)
        return self.results[self.current_index]

    def get_prev(self):
        if not self.results: return None
        self.current_index = (self.current_index - 1 + len(self.results)) % len(self.results)
        return self.results[self.current_index]

    def has_results(self):
        return bool(self.results)


# =============================================================================
# --- 业务逻辑控制器 (Business Logic Controller) ---
# =============================================================================

class AdbController:
    """封装所有ADB命令的执行逻辑"""

    def __init__(self, adb_path='adb'):
        self.adb_path = adb_path

    def _run_command(self, cmd, timeout=10, **kwargs):
        # 统一执行ADB命令的私有方法
        return subprocess.run(
            [self.adb_path] + cmd,
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='ignore',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            **kwargs
        )

    def get_devices(self):
        """获取设备列表"""
        try:
            result = self._run_command(['devices'], timeout=5)
            if result.returncode != 0:
                return f"ADB 错误: {result.stderr.strip()}"
            lines = [line for line in result.stdout.strip().splitlines() if
                     '\t' in line and 'List of devices' not in line]
            return lines if lines else "未找到设备"
        except subprocess.TimeoutExpired:
            return "获取设备列表超时"
        except Exception as e:
            return f"获取设备列表失败: {e}"

    def clear_logcat(self, serial):
        """清空设备日志"""
        try:
            self._run_command(['-s', serial, 'logcat', '-c'], timeout=3)
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "清空日志超时"
        except Exception as e:
            return False, f"清空日志失败: {e}"

    def get_pid(self, serial, package_name):
        """获取应用的PID"""
        try:
            result = self._run_command(['-s', serial, 'shell', f'pidof {package_name}'], timeout=5, check=True)
            pids = [p for p in result.stdout.strip().split() if p.isdigit()]
            if not pids:
                return None, f"应用 '{package_name}' 未运行或包名错误"
            return pids[-1], f"正在过滤包 {package_name} (PID: {pids[-1]})"
        except subprocess.CalledProcessError:
            return None, f"无法获取PID，请确认应用 '{package_name}' 正在运行"
        except subprocess.TimeoutExpired:
            return None, "获取PID超时"
        except Exception as e:
            return None, f"获取PID时发生未知错误: {e}"

    def build_logcat_command(self, serial, pid=None):
        """构建logcat命令"""
        cmd = [self.adb_path, '-s', serial, 'logcat']
        if pid:
            cmd.extend(['--pid', pid])
        return cmd

    def pull_log(self, serial, package_name, dest_path):
        """从设备拉取日志文件"""
        src = f"/storage/emulated/0/Android/data/{package_name}/files/outLog.txt"
        try:
            self._run_command(["-s", serial, "pull", src, dest_path], timeout=30, check=True)
            return True, f"日志导出成功:\n{os.path.abspath(dest_path)}"
        except subprocess.CalledProcessError as e:
            return False, f"导出失败，文件可能不存在或无权限:\n{e.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return False, "导出超时，请检查文件大小和设备连接"
        except Exception as e:
            return False, f"导出失败: {e}"


# 多线程日志
class LogThread(threading.Thread):
    def __init__(self, parent, cmd):
        super().__init__(daemon=True)
        self.parent = weakref.ref(parent)
        self.cmd = cmd
        self.stop_event = threading.Event()
        self.proc = None
        self.buffer = []
        self.last_flush = time.time()

    def run(self):
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='ignore',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            for line in iter(self.proc.stdout.readline, ''):
                if self.stop_event.is_set(): break
                self.buffer.append(line)
                if len(self.buffer) >= BUFFER_SIZE or time.time() - self.last_flush >= FLUSH_INTERVAL:
                    parent = self.parent()
                    if parent: wx.CallAfter(parent.update_log_text, self.buffer.copy())
                    self.buffer.clear()
                    self.last_flush = time.time()
        except Exception as e:
            parent = self.parent()
            if parent: wx.CallAfter(parent.log_thread_error, str(e))
        finally:
            parent = self.parent()
            if parent and self.buffer:
                wx.CallAfter(parent.update_log_text, self.buffer.copy())
            if self.proc:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            if parent: wx.CallAfter(parent.on_logging_stopped)

    def stop(self):
        self.stop_event.set()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass


# 包名列表对话框
class PackageListDialog(wx.Dialog):
    def __init__(self, parent, adb_path, serial):
        super().__init__(parent, title="选择应用包名", size=(500, 600))
        self.adb_path = adb_path
        self.serial = serial
        self.selected_package = None
        self.alias = ""
        self.init_ui()
        self.CentreOnParent()
        self.load_packages_in_thread()

    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(wx.StaticText(panel, label="设备上的第三方应用包名："), 0, wx.ALL, 10)
        self.package_list_box = wx.ListBox(panel, style=wx.LB_SINGLE | wx.LB_SORT)
        self.package_list_box.Bind(wx.EVT_LISTBOX, self.on_package_select)
        main_sizer.Add(self.package_list_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        alias_sizer = wx.BoxSizer(wx.HORIZONTAL)
        alias_sizer.Add(wx.StaticText(panel, label="设置别名 (可选):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.alias_input = wx.TextCtrl(panel)
        alias_sizer.Add(self.alias_input, 1, wx.LEFT, 5)
        main_sizer.Add(alias_sizer, 0, wx.EXPAND | wx.ALL, 10)
        button_sizer = wx.StdDialogButtonSizer()
        self.ok_button = wx.Button(panel, wx.ID_OK)
        self.ok_button.Disable()
        cancel_button = wx.Button(panel, wx.ID_CANCEL)
        button_sizer.AddButton(self.ok_button)
        button_sizer.AddButton(cancel_button)
        button_sizer.Realize()
        main_sizer.Add(button_sizer, 0, wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, 10)
        panel.SetSizer(main_sizer)
        self.Bind(wx.EVT_BUTTON, self.on_ok, id=wx.ID_OK)

    def load_packages_in_thread(self):
        self.package_list_box.Append("正在加载，请稍候...")
        threading.Thread(target=self._fetch_packages, daemon=True).start()

    def _fetch_packages(self):
        try:
            cmd = [self.adb_path, "-s", self.serial, "shell", "pm", "list", "packages", "-3"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode == 0:
                device_packages = [line.replace("package:", "").strip() for line in result.stdout.splitlines() if
                                   line.strip()]
                existing_packages = {p for _, p in ConfigManager.parse_packages(PACKAGES_FILE)}
                new_packages = [pkg for pkg in device_packages if pkg not in existing_packages]
                if not new_packages:
                    wx.CallAfter(self._show_error, f"设备上 {len(device_packages)} 个应用都已存在于 {PACKAGES_FILE} 中")
                else:
                    wx.CallAfter(self._update_list, new_packages)
            else:
                wx.CallAfter(self._show_error, f"获取包名失败:\n{result.stderr.strip()}")
        except Exception as e:
            wx.CallAfter(self._show_error, f"执行ADB命令时出错: {e}")

    def _update_list(self, packages):
        self.package_list_box.Clear()
        if packages:
            self.package_list_box.AppendItems(packages)
        else:
            self.package_list_box.Append("未找到新的第三方应用")

    def _show_error(self, message):
        self.package_list_box.Clear()
        self.package_list_box.Append(message)

    def on_package_select(self, event):
        self.selected_package = self.package_list_box.GetStringSelection()
        self.alias_input.SetValue(self.selected_package)
        self.ok_button.Enable(True)

    def on_ok(self, event):
        self.alias = self.alias_input.GetValue().strip()
        self.EndModal(wx.ID_OK)

    def get_selection(self):
        return self.alias, self.selected_package


class CatchLogFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="IKUN LogCatch V3.0", size=(900, 700))
        os.makedirs(LOGS_DIR, exist_ok=True)
        try:
            self.SetIcon(resources.AppIcon.GetIcon())
        except Exception as e:
            print(f"设置图标失败: {e}")

        # --- 初始化控制器和状态 ---
        self.adb_controller = AdbController()
        self.packages = []
        self.log_thread = None
        self.is_logging = False
        self.session_log_file = None
        self.session_log_path = os.path.join(LOGS_DIR, SESSION_LOG_FILENAME)

        # 搜索状态
        self.search_term = ""
        self.search_manager = SearchResultManager()
        self.search_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_execute_search, self.search_timer)

        # --- 构建UI ---
        self.init_ui()
        self.Centre()
        self.CreateStatusBar()

        # --- UI初始化后操作 ---
        self.update_package_combo()
        self.refresh_devices()
        self.Bind(wx.EVT_CLOSE, self.on_close)

    # UI
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        config_sizer = self._create_config_sizer(panel)
        control_sizer = self._create_control_sizer(panel)

        self.logCtrl = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_RICH2 | wx.TE_READONLY)
        self.logCtrl.SetFont(wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.logCtrl.Bind(wx.EVT_MOUSEWHEEL, self.on_log_scroll)
        self.logCtrl.Bind(wx.EVT_KEY_DOWN, self.on_log_scroll)

        self.default_style = wx.TextAttr("black", "white")
        self.highlight_style = wx.TextAttr("black", "yellow")
        self.primary_highlight_style = wx.TextAttr("white", "blue")

        main_sizer.Add(config_sizer, 0, wx.EXPAND | wx.ALL, 10)
        main_sizer.Add(control_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        main_sizer.Add(self.logCtrl, 1, wx.EXPAND | wx.ALL, 5)

        panel.SetSizer(main_sizer)

    def _create_config_sizer(self, panel):
        """创建顶部配置区域 (ADB, 设备, 包名)"""
        grid = wx.FlexGridSizer(3, 2, 5, 5)
        grid.AddGrowableCol(1)
        # adb
        self.adbCombo = wx.ComboBox(panel, style=wx.CB_READONLY)
        self.adbCombo.Append('使用环境变量', 'adb')
        self.adbCombo.AppendItems(ConfigManager.parse_adb_paths(ADB_PATHS_FILE))
        self.adbCombo.SetSelection(0)
        self.adbCombo.Bind(wx.EVT_COMBOBOX, self.on_adb_select)
        grid.Add(wx.StaticText(panel, label="ADB 路径:"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.adbCombo, 1, wx.EXPAND)
        # 设备选择
        self.deviceChoice = wx.Choice(panel)
        self.refreshBtn = wx.Button(panel, label="刷新")
        self.refreshBtn.Bind(wx.EVT_BUTTON, self.refresh_devices)
        deviceSizer = wx.BoxSizer(wx.HORIZONTAL)
        deviceSizer.Add(self.deviceChoice, 1, wx.EXPAND)
        deviceSizer.Add(self.refreshBtn, 0, wx.LEFT, 5)
        grid.Add(wx.StaticText(panel, label="目标设备:"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(deviceSizer, 1, wx.EXPAND)

        # 包名
        self.packageCombo = wx.ComboBox(panel)
        self.packageCombo.Bind(wx.EVT_TEXT, self.on_package_change)
        self.packageCombo.Bind(wx.EVT_COMBOBOX, self.on_package_change)
        self.listPackagesBtn = wx.Button(panel, label="列出包名")
        self.listPackagesBtn.Bind(wx.EVT_BUTTON, self.on_list_packages)
        packageSizer = wx.BoxSizer(wx.HORIZONTAL)
        packageSizer.Add(self.packageCombo, 1, wx.EXPAND)
        packageSizer.Add(self.listPackagesBtn, 0, wx.LEFT, 5)
        grid.Add(wx.StaticText(panel, label="包名 (过滤/导出):"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(packageSizer, 1, wx.EXPAND)

        return grid

    def _create_control_sizer(self, panel):
        """创建中间控制区域 (按钮, 搜索)"""
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        # 主要控制按钮
        self.startButton = wx.Button(panel, label="开始抓取")
        self.startButton.Bind(wx.EVT_BUTTON, self.on_start_stop)
        self.clearBtn = wx.Button(panel, label="清空显示")
        self.clearBtn.Bind(wx.EVT_BUTTON, lambda e: self.clear_log())
        self.exportBtn = wx.Button(panel, label="导出ADB日志")
        self.exportBtn.Bind(wx.EVT_BUTTON, self.export_realtime)
        self.pullBtn = wx.Button(panel, label="导出应用日志")
        self.pullBtn.Bind(wx.EVT_BUTTON, self.export_package_log)
        self.autoScroll = wx.CheckBox(panel, label="自动滚动")
        self.autoScroll.SetValue(True)
        self.autoScroll.Bind(wx.EVT_CHECKBOX, self.on_autoscroll_checkbox_manual_toggle)

        for btn in [self.startButton, self.clearBtn, self.exportBtn, self.pullBtn]:
            sizer.Add(btn, 0, wx.ALL, 5)
        sizer.Add(self.autoScroll, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        sizer.AddStretchSpacer(1)

        # 搜索控件
        self.searchCtrl = wx.SearchCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.searchCtrl.SetDescriptiveText("全词匹配搜索")
        self.searchCtrl.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        self.searchCtrl.Bind(wx.EVT_TEXT, self.on_search)
        self.searchCtrl.ShowCancelButton(True)
        self.searchCtrl.Bind(wx.EVT_SEARCH_CANCEL, self.on_cancel_search)
        self.regexCheck = wx.CheckBox(panel, label="正则")
        self.regexCheck.Bind(wx.EVT_CHECKBOX, self.on_regex_check)
        self.prevBtn = wx.Button(panel, label="↑", size=(30, -1))
        self.nextBtn = wx.Button(panel, label="↓", size=(30, -1))
        self.prevBtn.Bind(wx.EVT_BUTTON, self.on_find_prev)
        self.nextBtn.Bind(wx.EVT_BUTTON, self.on_find_next)
        self.prevBtn.Disable()
        self.nextBtn.Disable()
        sizer.Add(self.searchCtrl, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 5)
        sizer.Add(self.regexCheck, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 5)
        sizer.Add(self.prevBtn, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 5)
        sizer.Add(self.nextBtn, 0, wx.ALIGN_CENTER_VERTICAL)

        return sizer

    def on_adb_select(self, event):
        """处理ADB路径选择变更"""
        selection = self.adbCombo.GetStringSelection()
        self.adb_controller.adb_path = "adb" if "环境变量" in selection else selection
        self.refresh_devices()

    def refresh_devices(self, event=None):
        """启动后台线程刷新设备列表"""
        self.refreshBtn.Disable()
        self.SetStatusText("正在刷新设备列表...")
        threading.Thread(target=self._do_refresh_devices, daemon=True).start()

    def _do_refresh_devices(self):
        """后台工作方法，从ADB控制器获取设备"""
        devices_or_error = self.adb_controller.get_devices()
        wx.CallAfter(self._update_device_list, devices_or_error)
        wx.CallAfter(self.refreshBtn.Enable)

    def _update_device_list(self, result):
        """在UI线程中更新设备下拉列表"""
        self.deviceChoice.Clear()
        if isinstance(result, str):  # 错误或无设备
            self.deviceChoice.Append(result)
            self.SetStatusText(result)
        else:  # 设备列表
            for line in result:
                dev_name = line.strip().split('\t')[0]
                self.deviceChoice.Append(dev_name, line)
            self.deviceChoice.SetSelection(0)
            self.SetStatusText(f"找到 {len(result)} 个设备")

    def on_package_change(self, event):
        """从包名输入框更新内部包名状态"""
        value = self.packageCombo.GetValue()
        match = re.search(r'\((.*?)\)', value)
        self.package_name = match.group(1) if match else value.strip()

    def on_list_packages(self, event):
        """打开对话框以列出并添加新包名"""
        idx = self.deviceChoice.GetSelection()
        if idx == wx.NOT_FOUND or "设备" in self.deviceChoice.GetStringSelection().lower():
            wx.MessageBox("请先从'目标设备'下拉列表中选择一个有效设备。", "提示", wx.OK | wx.ICON_INFORMATION)
            return

        serial = self.deviceChoice.GetClientData(idx).split('\t')[0]
        self.listPackagesBtn.Disable()
        try:
            with PackageListDialog(self, self.adb_controller.adb_path, serial) as dlg:
                if dlg.ShowModal() == wx.ID_OK:
                    alias, package = dlg.get_selection()
                    if package:
                        new_entry = f"{alias} {package}" if alias and alias != package else package
                        try:
                            with open(PACKAGES_FILE, "a", encoding="utf-8") as f:
                                f.write(f"\n{new_entry}")
                            self.update_package_combo(select_value=f"{alias or package} ({package})")
                        except IOError as e:
                            wx.MessageBox(f"无法写入 '{PACKAGES_FILE}': {e}", "文件错误", wx.OK | wx.ICON_ERROR)
        finally:
            self.listPackagesBtn.Enable()

    def update_package_combo(self, select_value=None):
        """重新加载并更新包名下拉框"""
        ConfigManager.parse_packages.cache_clear()
        self.packages = ConfigManager.parse_packages(PACKAGES_FILE)
        pkg_display = [f"{n} ({p})" if n != p else n for n, p in self.packages]

        current_val = self.packageCombo.GetValue()
        self.packageCombo.Clear()
        self.packageCombo.AppendItems(pkg_display)

        if select_value and select_value in pkg_display:
            self.packageCombo.SetValue(select_value)
        elif current_val in pkg_display:
            self.packageCombo.SetValue(current_val)
        elif pkg_display:
            self.packageCombo.SetSelection(0)

        self.on_package_change(None)

    def on_start_stop(self, event):
        """开始或停止日志抓取线程"""
        if self.is_logging:
            if self.log_thread: self.log_thread.stop()
            self.startButton.Enable(False)
            self.SetStatusText("正在停止日志抓取...")
            return

        idx = self.deviceChoice.GetSelection()
        if idx == wx.NOT_FOUND or "设备" in self.deviceChoice.GetStringSelection().lower():
            wx.MessageBox("请选择一个有效设备。", "提示")
            return

        serial = self.deviceChoice.GetClientData(idx).split('\t')[0]

        success, msg = self.adb_controller.clear_logcat(serial)
        if not success: wx.MessageBox(msg, "警告")

        pid = None
        pkg = self.package_name.strip()
        if pkg:
            pid, status_msg = self.adb_controller.get_pid(serial, pkg)
            if not pid:
                wx.MessageBox(status_msg, "错误")
                return
            self.SetStatusText(status_msg)

        try:
            self.session_log_file = open(self.session_log_path, 'w', encoding='utf-8')
        except IOError as e:
            wx.MessageBox(f"无法创建会话日志文件: {e}", "文件错误")
            return

        self.clear_log()
        self.is_logging = True
        self.startButton.SetLabel("停止抓取")
        self.toggle_controls(False)

        cmd = self.adb_controller.build_logcat_command(serial, pid)
        self.log_thread = LogThread(self, cmd)
        self.log_thread.start()
        self.SetStatusText("日志抓取中...")

    def on_logging_stopped(self):
        self.is_logging = False
        self.log_thread = None
        if self.session_log_file:
            try:
                self.session_log_file.close()
            except Exception as e:
                print(f"Error closing session log file: {e}")
            self.session_log_file = None
        self.startButton.SetLabel("开始抓取")
        self.startButton.Enable(True)
        self.toggle_controls(True)
        self.SetStatusText("日志停止。")

    def toggle_controls(self, enable):
        """在抓取日志期间启用或禁用相关控件"""
        self.deviceChoice.Enable(enable)
        self.packageCombo.Enable(enable)
        self.adbCombo.Enable(enable)
        self.refreshBtn.Enable(enable)
        self.listPackagesBtn.Enable(enable)

    def update_log_text(self, lines, text_to_append=""):
        scroll_status = self._get_view_status()
        try:
            temp_lines = "".join(lines)
            if self.session_log_file and not self.session_log_file.closed:
                try:
                    self.session_log_file.write(temp_lines)
                except Exception as e:
                    print(f"Error writing to session log: {e}")
            text_to_append += temp_lines
            if scroll_status and self.autoScroll.IsChecked():
                self.logCtrl.AppendText(text_to_append)
                current_length = self.logCtrl.GetLastPosition()
                if current_length > MAX_CHARS:
                    deleted_count = current_length - (MAX_CHARS // 2)
                    self.logCtrl.Remove(0, deleted_count)
                if self.search_term:
                    start_highlight_pos = self.logCtrl.GetLastPosition() - len(text_to_append)
                    self._update_highlights(self.search_term, start_highlight_pos, self.logCtrl.GetLastPosition())
                text_to_append = ""
        except wx.wxAssertionError:
            pass
        except Exception as e:
            print(f"更新日志时出错: {e}")

    def clear_log(self):
        self.logCtrl.Clear()
        self._reset_search_state()

    def export_realtime(self, event):
        """导出临时会话日志文件的内容"""
        name = f"logcat_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path = os.path.join(LOGS_DIR, name)

        if not os.path.exists(self.session_log_path):
            wx.MessageBox("没有日志内容可导出。", "提示")
            return

        try:
            if self.is_logging and self.session_log_file:
                self.session_log_file.flush()
            shutil.copy2(self.session_log_path, path)
            self.SetStatusText(f"成功导出到 {path}")
        except Exception as e:
            wx.MessageBox(f"导出失败: {e}", "错误")

    def export_package_log(self, event):
        """在后台线程中从设备拉取应用内日志文件"""
        if not self.package_name:
            wx.MessageBox("请输入或选择一个包名。", "提示")
            return
        idx = self.deviceChoice.GetSelection()
        if idx == wx.NOT_FOUND or "设备" in self.deviceChoice.GetStringSelection().lower():
            wx.MessageBox("请选择一个有效设备。", "提示")
            return

        serial = self.deviceChoice.GetClientData(idx).split('\t')[0]
        dest_dir = os.path.join(LOGS_DIR, self.package_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, f"outLog_{time.strftime('%Y%m%d_%H%M%S')}.txt")

        self.pullBtn.Disable()
        self.SetStatusText(f"正在为 {self.package_name} 拉取日志...")
        threading.Thread(target=self._do_export_package_log, args=(serial, self.package_name, dest_path),
                         daemon=True).start()

    def _do_export_package_log(self, serial, package_name, dest_path):
        """后台工作方法，用于拉取日志文件"""
        success, message = self.adb_controller.pull_log(serial, package_name, dest_path)
        wx.CallAfter(self._on_export_package_log_done, success, message)

    def _on_export_package_log_done(self, success, message):
        """在UI线程中处理日志拉取完成事件"""
        self.pullBtn.Enable()
        self.SetStatusText("拉取完成。")
        title = "成功" if success else "错误"
        style = wx.ICON_INFORMATION if success else wx.ICON_ERROR
        wx.MessageBox(message, title, wx.OK | style)

    def _get_view_status(self):
        """
        获取当前视图状态。
        返回: (bool is_at_bottom, int current_vis_bottom_line)
        """
        total_lines = self.logCtrl.GetNumberOfLines() - 1
        width, height = self.logCtrl.GetClientSize()
        res = self.logCtrl.HitTest((5, height - 5))  # 这里的 5 像素偏移是为了避开边框
        if res[0] != wx.TE_HT_UNKNOWN:
            current_vis_bottom_line = res[2]
        else:
            # 如果检测失败（比如空窗口），假设我们在最后一行
            current_vis_bottom_line = total_lines
        is_at_bottom = (current_vis_bottom_line == total_lines)
        return is_at_bottom

    def on_log_scroll(self, event):
        """
        处理用户滚动事件，其唯一职责是安排一个后续任务来同步复选框状态。
        """
        event.Skip()
        wx.CallAfter(self.sync_autoscroll_checkbox_to_view)

    def on_autoscroll_checkbox_manual_toggle(self, event):
        """如果手动勾选复选框，则滚动到底部"""
        if self.autoScroll.IsChecked():
            self.logCtrl.ShowPosition(self.logCtrl.GetLastPosition())
        event.Skip()

    def sync_autoscroll_checkbox_to_view(self):
        """
        将“自动滚动”复选框的状态与日志控件的滚动位置同步。
        这是实现“双向绑定”的关键。
        """
        is_at_bottom = self._get_view_status()
        if self.autoScroll.IsChecked() != is_at_bottom:
            self.autoScroll.SetValue(is_at_bottom)

    # --- 搜索与高亮逻辑 ---

    @functools.lru_cache(maxsize=128)
    def _get_compiled_pattern(self, pattern_str, is_regex):
        """编译并缓存用于搜索的正则表达式"""
        final_pattern = pattern_str if is_regex else r'\b' + re.escape(pattern_str) + r'\b'
        try:
            flags = re.IGNORECASE if not any(c.isupper() for c in pattern_str) else 0
            return re.compile(final_pattern, flags)
        except re.error:
            return None

    def on_regex_check(self, event):
        """处理正则复选框切换"""
        self.searchCtrl.SetDescriptiveText("正则搜索" if self.regexCheck.IsChecked() else "全词匹配搜索")
        if self.searchCtrl.GetValue(): self.on_execute_search(None)

    def on_search(self, event):
        """在文本变化后延迟搜索，或在回车时立即搜索"""
        self.search_timer.Stop()
        if event.GetEventType() == wx.EVT_TEXT_ENTER.typeId:
            self.on_execute_search(event)
        else:
            self.search_timer.StartOnce(SEARCH_DELAY)

    def on_execute_search(self, event):
        """执行实际的搜索操作"""
        term = self.searchCtrl.GetValue().strip()
        if self.search_term == term and event is not None: return
        self._reset_search_state()
        self.search_term = term
        if not term:
            self.on_cancel_search(None)
            return
        self._update_highlights(term, 0, self.logCtrl.GetLastPosition(), initial_scan=True)
        if self.search_manager.has_results():
            self._jump_to_current_match()
            self.prevBtn.Enable()
            self.nextBtn.Enable()
        else:
            self.SetStatusText(f"'{term}' 无匹配项")
            self.prevBtn.Disable()
            self.nextBtn.Disable()
        wx.CallAfter(self.sync_autoscroll_checkbox_to_view)

    def _update_highlights(self, term, start_pos, end_pos, initial_scan=False):
        """在指定文本范围内查找并应用高亮。"""
        is_regex = self.regexCheck.IsChecked()
        pattern = self._get_compiled_pattern(term, is_regex)
        if not pattern:
            if is_regex: self.SetStatusText(f"无效的正则表达式: {term}")
            return
        try:
            content = self.logCtrl.GetRange(start_pos, end_pos)
        except wx.wxAssertionError:
            return
        if not content: return
        if initial_scan:
            self.search_manager.clear()
            self._clear_all_highlights()
        try:
            for m in pattern.finditer(content):
                s, e = start_pos + m.start(), start_pos + m.end()
                self.logCtrl.SetStyle(s, e, self.highlight_style)
                self.search_manager.add_result(s, e)
        except (Exception, wx.wxAssertionError):
            return
        if self.search_manager.has_results():
            self.SetStatusText(f"为 '{self.search_term}' 找到 {len(self.search_manager.results)} 个匹配项")

    def on_find_next(self, event):
        if not self.search_manager.has_results(): return
        old_index = self.search_manager.current_index
        if self.search_manager.get_next():
            self._jump_to_current_match(old_index)
        wx.CallAfter(self.sync_autoscroll_checkbox_to_view)

    def on_find_prev(self, event):
        if not self.search_manager.has_results(): return
        old_index = self.search_manager.current_index
        if self.search_manager.get_prev():
            self._jump_to_current_match(old_index)
        wx.CallAfter(self.sync_autoscroll_checkbox_to_view)

    def _jump_to_current_match(self, old_index=None):
        """高亮搜索匹配项并滚动到该位置"""
        if old_index is not None and old_index != self.search_manager.current_index:
            try:
                prev_s, prev_e = self.search_manager.results[old_index]
                self.logCtrl.SetStyle(prev_s, prev_e, self.highlight_style)
            except (IndexError, wx.wxAssertionError):
                pass
        try:
            s, e = self.search_manager.results[self.search_manager.current_index]
            self.logCtrl.SetStyle(s, e, self.primary_highlight_style)
            self.logCtrl.ShowPosition(s)
            self.logCtrl.SetInsertionPoint(s)
            self.SetStatusText(
                f"匹配项 {self.search_manager.current_index + 1}/{len(self.search_manager.results)}: '{self.search_term}'")
        except (IndexError, wx.wxAssertionError):
            self._reset_search_state()

    def on_cancel_search(self, event):
        """清除搜索状态和高亮"""
        self.search_timer.Stop()
        if not self.search_term: return
        self._clear_all_highlights()
        self._reset_search_state()
        self.prevBtn.Disable()
        self.nextBtn.Disable()
        self.searchCtrl.SetValue("")
        self.SetStatusText("搜索已取消。")

    def _clear_all_highlights(self):
        try:
            self.logCtrl.SetStyle(0, self.logCtrl.GetLastPosition(), self.default_style)
        except wx.wxAssertionError:
            pass

    def _reset_search_state(self):
        self.search_manager.clear()
        self.search_term = ""
        self._get_compiled_pattern.cache_clear()

    def log_thread_error(self, msg):
        wx.MessageBox(f"日志线程出错:\n{msg}", "错误")
        self.on_logging_stopped()

    def on_close(self, event):
        if self.log_thread and self.log_thread.is_alive():
            self.log_thread.stop()
        if os.path.exists(self.session_log_path):
            try:
                os.remove(self.session_log_path)
            except OSError as e:
                print(f"移除会话日志文件时出错: {e}")
        self.Destroy()


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
