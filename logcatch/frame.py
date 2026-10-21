import wx
import os
import re
import subprocess
import threading
import time
import functools
from pathlib import Path

from .config import (ConfigManager, LOGS_DIR, SESSION_LOG_FILENAME, PACKAGES_FILE,
                     ADB_PATHS_FILE, MAX_CHARS, SEARCH_DELAY)
from .search import SearchResultManager
from .adb import AdbController
from .workers import LogThread, FilePollingThread
from .dialogs import PackageListDialog, RemoteFilePickerDialog
from .session import SessionLog
from .version import get_version
from .logview import LogView

class CatchLogFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=f"IKUN LogCatch V{get_version()}", size=(1050, 750))
        os.makedirs(LOGS_DIR, exist_ok=True)
        try:
            icon_path = Path(__file__).resolve().parent.parent / 'img' / 'ikun.ico'
            self.SetIcon(wx.Icon(str(icon_path), wx.BITMAP_TYPE_ICO))
        except Exception as e:
            print(f"设置图标失败: {e}")

        # --- 初始化控制器和状态 ---
        self.adb_controller = AdbController()
        self.packages = []
        self.log_thread = None
        self.is_logging = False
        self.is_device_busy = False
        self.last_device_address = "127.0.0.1:5555"
        self.session_log_path = os.path.join(LOGS_DIR, SESSION_LOG_FILENAME)

        self.session = SessionLog(self.session_log_path)
        self.session_id = 0
        self.closing = False
        self.pending_errors = []

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

        self.logCtrl = LogView(panel, self.on_log_view_scroll)

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
        self.connectBtn = wx.Button(panel, label="主动连接")
        self.connectBtn.Bind(wx.EVT_BUTTON, self.on_connect_device)
        deviceSizer = wx.BoxSizer(wx.HORIZONTAL)
        deviceSizer.Add(self.deviceChoice, 1, wx.EXPAND)
        deviceSizer.Add(self.refreshBtn, 0, wx.LEFT, 5)
        deviceSizer.Add(self.connectBtn, 0, wx.LEFT, 5)
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
        """创建中间控制区域 (按钮, 搜索) - 优化布局为两行"""
        main_ctrl_sizer = wx.BoxSizer(wx.VERTICAL)

        # 第一行：功能按钮
        row1_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.startButton = wx.Button(panel, label="开始抓取")
        self.startButton.Bind(wx.EVT_BUTTON, self.on_start_stop)
        self.clearBtn = wx.Button(panel, label="清空显示")
        self.clearBtn.Bind(wx.EVT_BUTTON, lambda e: self.clear_log())
        self.exportBtn = wx.Button(panel, label="导出ADB日志")
        self.exportBtn.Bind(wx.EVT_BUTTON, self.export_realtime)
        self.pullBtn = wx.Button(panel, label="导出应用日志")
        self.pullBtn.Bind(wx.EVT_BUTTON, self.export_package_log)
        self.monitorBtn = wx.Button(panel, label="查看文件")
        self.monitorBtn.Bind(wx.EVT_BUTTON, self.on_monitor_file)
        self.reloadBtn = wx.Button(panel, label="刷新")
        self.reloadBtn.Bind(wx.EVT_BUTTON, self.on_reload_file)
        self.reloadBtn.Disable()
        self.openLogsBtn = wx.Button(panel, label="打开日志目录")
        self.openLogsBtn.Bind(wx.EVT_BUTTON, self.on_open_logs_dir)

        for btn in [self.startButton, self.clearBtn, self.exportBtn, self.pullBtn, self.monitorBtn, self.reloadBtn, self.openLogsBtn]:
            row1_sizer.Add(btn, 0, wx.RIGHT, 5)

        row1_sizer.AddStretchSpacer(1)

        # 第二行：设置和搜索
        row2_sizer = wx.BoxSizer(wx.HORIZONTAL)

        row2_sizer.Add(wx.StaticText(panel, label="编码:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.encodingChoice = wx.Choice(panel, choices=["UTF-8", "GBK"])
        self.encodingChoice.SetSelection(0)
        row2_sizer.Add(self.encodingChoice, 0, wx.RIGHT, 15)

        self.autoScroll = wx.CheckBox(panel, label="自动滚动")
        self.autoScroll.SetValue(True)
        self.autoScroll.Bind(wx.EVT_CHECKBOX, self.on_autoscroll_checkbox_manual_toggle)
        row2_sizer.Add(self.autoScroll, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)

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

        row2_sizer.Add(self.searchCtrl, 1, wx.EXPAND | wx.RIGHT, 5)
        row2_sizer.Add(self.regexCheck, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        row2_sizer.Add(self.prevBtn, 0, wx.RIGHT, 5)
        row2_sizer.Add(self.nextBtn, 0)

        main_ctrl_sizer.Add(row1_sizer, 0, wx.EXPAND | wx.BOTTOM, 8)
        main_ctrl_sizer.Add(row2_sizer, 0, wx.EXPAND | wx.BOTTOM, 5)

        return main_ctrl_sizer

    def on_adb_select(self, event):
        """处理ADB路径选择变更"""
        selection = self.adbCombo.GetStringSelection()
        self.adb_controller.adb_path = "adb" if "环境变量" in selection else selection
        self.refresh_devices()

    def refresh_devices(self, event=None):
        """启动后台线程刷新设备列表"""
        if self.is_device_busy or self.is_logging:
            return
        self.is_device_busy = True
        self.toggle_controls(False)
        self.startButton.Disable()
        self.SetStatusText("正在刷新设备列表...")
        threading.Thread(target=self._do_refresh_devices, daemon=True).start()

    def _do_refresh_devices(self):
        """后台工作方法，从ADB控制器获取设备"""
        devices_or_error = self.adb_controller.get_devices()
        wx.CallAfter(self._on_device_operation_done, devices_or_error)

    def on_connect_device(self, event):
        if self.is_device_busy or self.is_logging:
            return
        with wx.TextEntryDialog(self, "输入设备 IP/主机名和端口（默认 5555）：",
                                "主动连接设备", self.last_device_address) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            address = dlg.GetValue().strip()
        if not address:
            wx.MessageBox("请输入设备地址。", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        self.last_device_address = address
        self.is_device_busy = True
        self.toggle_controls(False)
        self.startButton.Disable()
        self.SetStatusText(f"正在连接 {address}...")
        threading.Thread(target=self._do_connect_device, args=(address,), daemon=True).start()

    def _do_connect_device(self, address):
        success, message, serial = self.adb_controller.connect_device(address)
        devices = self.adb_controller.get_devices() if success else None
        wx.CallAfter(self._on_device_operation_done, devices, serial, success, message)

    def _on_device_operation_done(self, devices, serial=None, success=None, message=""):
        if self.closing:
            return
        self.is_device_busy = False
        self.toggle_controls(not self.is_logging)
        self.startButton.Enable()
        if devices is not None:
            self._update_device_list(devices, serial)
        if success is not None:
            self.SetStatusText(message)
            if not success:
                wx.MessageBox(message, "连接失败", wx.OK | wx.ICON_ERROR)

    def _update_device_list(self, result, selected_serial=None):
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
            if selected_serial:
                for idx in range(self.deviceChoice.GetCount()):
                    if self.deviceChoice.GetString(idx) == selected_serial:
                        self.deviceChoice.SetSelection(idx)
                        break
            self.SetStatusText(f"找到 {len(result)} 个设备")

    def on_monitor_file(self, event):
        """选择并实时监控应用内部文件"""
        serial = self._selected_serial()
        if not serial:
            return
        pkg = self.package_name.strip()

        if not pkg:
            wx.MessageBox("请先输入或从下拉框选择包名。", "提示", wx.OK | wx.ICON_INFORMATION)
            return

        with RemoteFilePickerDialog(self, self.adb_controller, serial, pkg) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                remote_path = dlg.get_selected_file()
                if remote_path:
                    self.start_file_monitoring(serial, remote_path)

    def on_reload_file(self, event):
        """手动触发文件刷新"""
        if self.is_logging and isinstance(self.log_thread, FilePollingThread):
            self.log_thread.trigger_refresh()
            self.SetStatusText(f"已发送刷新请求: {time.strftime('%H:%M:%S')}")

    def start_file_monitoring(self, serial, remote_path):
        if self.is_logging:
            return
        if not self._start_session():
            return
        self.clear_log()
        enc = self.encodingChoice.GetStringSelection().lower()
        self.log_thread = FilePollingThread(
            self.adb_controller, serial, remote_path,
            self._session_callback(self.refresh_file_content),
            self._session_callback(self.file_refresh_error),
            self._session_callback(self.on_logging_stopped), encoding=enc)
        self.log_thread.start()
        self.reloadBtn.Enable()
        self.SetStatusText(f"文件快照（末尾 1000 行，手动刷新）: {remote_path}")

    def _start_session(self):
        try:
            self.session.start()
        except OSError as exc:
            wx.MessageBox(f"无法创建会话日志文件: {exc}", "文件错误")
            return False
        self.session_id += 1
        self.pending_errors.clear()
        self.is_logging = True
        self.startButton.SetLabel("停止抓取")
        self.toggle_controls(False)
        return True

    def _session_callback(self, callback):
        session_id = self.session_id
        def dispatch(*args):
            if not self.closing:
                wx.CallAfter(self._deliver_session, session_id, callback, args)
        return dispatch

    def _deliver_session(self, session_id, callback, args):
        if not self.closing and self.is_logging and session_id == self.session_id:
            try:
                callback(*args)
            except OSError as exc:
                self.log_thread_error(f'会话日志写入失败: {exc}')
                if self.log_thread:
                    self.log_thread.stop()

    def file_refresh_error(self, message):
        self.SetStatusText(message)

    def refresh_file_content(self, content):
        if not self.is_logging:
            return
        self.session.snapshot(content)
        self.logCtrl.replace_content(content, MAX_CHARS, self.autoScroll.IsChecked())
        if self.search_term:
            self._refresh_search()

    def on_open_logs_dir(self, event):

        """打开本地日志存储目录"""
        abs_path = os.path.abspath(LOGS_DIR)
        if not os.path.exists(abs_path):
            os.makedirs(abs_path, exist_ok=True)

        if os.name == 'nt':
            os.startfile(abs_path)
        else:
            # 兼容其他系统
            try:
                subprocess.run(['open' if os.uname().sysname == 'Darwin' else 'xdg-open', abs_path])
            except Exception as e:
                print(f"打开目录失败: {e}")

    def on_package_change(self, event):
        """从包名输入框更新内部包名状态"""
        value = self.packageCombo.GetValue()
        match = re.search(r'\((.*?)\)', value)
        self.package_name = match.group(1) if match else value.strip()

    def on_list_packages(self, event):
        """打开对话框以列出并添加新包名"""
        serial = self._selected_serial()
        if not serial:
            return
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
        if self.is_device_busy:
            return
        if self.is_logging:
            if self.log_thread:
                self.log_thread.stop()
            self.startButton.Disable()
            self.reloadBtn.Disable()
            self.SetStatusText("正在停止日志抓取...")
            return
        serial = self._selected_serial()
        if not serial:
            return
        # PID and log clearing can wait on ADB; keep them off the UI thread.
        self.is_device_busy = True
        self.toggle_controls(False)
        self.startButton.Disable()
        self.SetStatusText("正在准备日志抓取...")
        pkg = self.package_name.strip()
        def prepare():
            pid, error, warning = None, None, None
            if pkg:
                pid, message = self.adb_controller.get_pid(serial, pkg)
                if not pid:
                    error = message
            if error is None:
                success, message = self.adb_controller.clear_logcat(serial)
                if not success:
                    warning = message
            wx.CallAfter(self._finish_prepare, serial, pid, error, warning)
        threading.Thread(target=prepare, daemon=True).start()

    def _finish_prepare(self, serial, pid, error, warning):
        if self.closing:
            return
        self.is_device_busy = False
        self.toggle_controls(True)
        self.startButton.Enable()
        if error:
            self.SetStatusText(error)
            wx.MessageBox(error, "错误")
            return
        if warning:
            wx.MessageBox(warning, "警告")
        if not self._start_session():
            return
        self.clear_log()
        cmd = self.adb_controller.build_logcat_command(serial, pid)
        enc = self.encodingChoice.GetStringSelection().lower()
        self.log_thread = LogThread(
            cmd, self._session_callback(self.update_log_text),
            self._session_callback(self.log_thread_error),
            self._session_callback(self.on_logging_stopped), encoding=enc)
        self.log_thread.start()
        self.SetStatusText("日志抓取中...")

    def _selected_serial(self):
        idx = self.deviceChoice.GetSelection()
        data = self.deviceChoice.GetClientData(idx) if idx != wx.NOT_FOUND else None
        if not isinstance(data, str) or '\t' not in data:
            wx.MessageBox("请选择一个有效设备。", "提示")
            return None
        serial, state = data.split('\t', 1)
        if state.strip() != 'device':
            wx.MessageBox(f"设备不可用: {state.strip()}", "提示")
            return None
        return serial

    def on_logging_stopped(self):
        try:
            self.session.close()
        except OSError as exc:
            self.pending_errors.append(f'会话日志关闭失败: {exc}')
        self.is_logging = False
        self.log_thread = None
        self.session_id += 1
        self.startButton.SetLabel("开始抓取")
        self.startButton.Enable()
        self.reloadBtn.Disable()
        self.toggle_controls(True)
        message = '; '.join(self.pending_errors)
        self.pending_errors.clear()
        self.SetStatusText(message or "日志停止。")
        if message:
            wx.MessageBox(message, "日志抓取失败")

    def toggle_controls(self, enable):
        """在抓取日志期间启用或禁用相关控件"""
        self.deviceChoice.Enable(enable)
        self.packageCombo.Enable(enable)
        self.adbCombo.Enable(enable)
        self.refreshBtn.Enable(enable)
        self.connectBtn.Enable(enable)
        self.listPackagesBtn.Enable(enable)
        self.monitorBtn.Enable(enable)
        self.encodingChoice.Enable(enable)

    def update_log_text(self, lines):
        content = "".join(lines)
        if not content:
            return
        self.session.append(content)
        removed = self.logCtrl.append_content(content, MAX_CHARS,
                                              self.autoScroll.IsChecked())
        if self.search_term:
            self._refresh_search(removed)

    def clear_log(self):
        self.logCtrl.clear_content()
        self._reset_search_state()
        self.searchCtrl.ChangeValue("")
        self.prevBtn.Disable()
        self.nextBtn.Disable()
        self._show_match_status()

    def export_realtime(self, event):
        name = f"logcat_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        destination = os.path.join(LOGS_DIR, name)
        try:
            self.session.export(destination)
            self.SetStatusText(f"成功导出到 {destination}")
        except Exception as exc:
            wx.MessageBox(f"导出失败: {exc}", "错误")

    def export_package_log(self, event):
        """在后台线程中从设备拉取应用内日志文件"""
        if not self.package_name:
            wx.MessageBox("请输入或选择一个包名。", "提示")
            return
        serial = self._selected_serial()
        if not serial:
            return
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
        if self.closing:
            return
        """在UI线程中处理日志拉取完成事件"""
        self.pullBtn.Enable()
        self.SetStatusText("拉取完成。")
        title = "成功" if success else "错误"
        style = wx.ICON_INFORMATION if success else wx.ICON_ERROR
        wx.MessageBox(message, title, wx.OK | style)

    def on_log_view_scroll(self, at_bottom):
        if not self.closing:
            self.autoScroll.SetValue(at_bottom)

    def on_autoscroll_checkbox_manual_toggle(self, event):
        if self.autoScroll.IsChecked():
            self.logCtrl.scroll_to_tail()
        event.Skip()

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
        term = self.searchCtrl.GetValue().strip()
        if self.search_term == term and event is not None:
            return
        self._reset_search_state()
        self.search_term = term
        if not term:
            self.on_cancel_search(None)
            return
        if not self._refresh_search():
            return
        if self.search_manager.has_results():
            self.search_manager.get_next()
            self._jump_to_current_match()

    def _refresh_search(self, removed_bytes=0):
        previous = None
        index = self.search_manager.current_index
        if 0 <= index < len(self.search_manager.results):
            start, end = self.search_manager.results[index]
            if start >= removed_bytes:
                previous = (start - removed_bytes, end - removed_bytes)
        is_regex = self.regexCheck.IsChecked()
        pattern = self._get_compiled_pattern(self.search_term, is_regex)
        results = self.logCtrl.highlight_matches(pattern)
        self.search_manager.clear()
        for span in results:
            self.search_manager.add_result(*span)
        if previous in results:
            self.search_manager.current_index = results.index(previous)
            self.logCtrl.mark_current_match(previous)
        has_results = self.search_manager.has_results()
        self.prevBtn.Enable(has_results)
        self.nextBtn.Enable(has_results)
        if pattern is None:
            self.SetStatusText(f"无效的正则表达式: {self.search_term}")
            return False
        self._show_match_status()
        return True

    def _show_match_status(self):
        current = self.search_manager.current_index + 1
        total = len(self.search_manager.results)
        term = f": '{self.search_term}'" if self.search_term else ""
        self.SetStatusText(f"匹配项 {current}/{total}{term}")

    def on_find_next(self, event):
        if self.search_manager.get_next() is not None:
            self._jump_to_current_match()

    def on_find_prev(self, event):
        if self.search_manager.get_prev() is not None:
            self._jump_to_current_match()

    def _jump_to_current_match(self):
        index = self.search_manager.current_index
        if not 0 <= index < len(self.search_manager.results):
            return
        span = self.search_manager.results[index]
        # Searching is explicit navigation; incoming logs must not pull the view away.
        self.autoScroll.SetValue(False)
        self.logCtrl.mark_current_match(span)
        self.logCtrl.reveal_match(*span)
        self._show_match_status()

    def on_cancel_search(self, event):
        self.search_timer.Stop()
        self._clear_all_highlights()
        self._reset_search_state()
        self.prevBtn.Disable()
        self.nextBtn.Disable()
        self.searchCtrl.ChangeValue("")
        self._show_match_status()

    def _clear_all_highlights(self):
        self.logCtrl.clear_highlights()

    def _reset_search_state(self):
        self.search_manager.clear()
        self.search_term = ""
        self._get_compiled_pattern.cache_clear()

    def log_thread_error(self, msg):
        self.pending_errors.append(msg)
        self.SetStatusText(msg)

    def on_close(self, event):
        self.closing = True
        self.session_id += 1
        self.search_timer.Stop()
        self.Hide()
        worker = self.log_thread
        if worker and worker.is_alive():
            worker.stop()
            def finish():
                worker.join()
                wx.CallAfter(self._finish_close)
            threading.Thread(target=finish, daemon=True).start()
        else:
            self._finish_close()

    def _finish_close(self):
        try:
            self.session.discard()
        except OSError as exc:
            wx.MessageBox(f'临时会话日志清理失败: {exc}', '文件错误')
        finally:
            self.Destroy()
