import wx
import threading
from .config import ConfigManager, PACKAGES_FILE
from .adb import AdbController

class PackageListDialog(wx.Dialog):
    def __init__(self, parent, adb_path, serial):
        super().__init__(parent, title="选择应用包名", size=(500, 600))
        self.adb_path = adb_path
        self.serial = serial
        self.valid_packages = set()
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
            result = AdbController(self.adb_path)._run_command(
                ['-s', self.serial, 'shell', 'pm', 'list', 'packages', '-3'], timeout=15)
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
        if not self:
            return
        self.valid_packages = set(packages)
        self.package_list_box.Clear()
        if packages:
            self.package_list_box.AppendItems(packages)
        else:
            self.package_list_box.Append("未找到新的第三方应用")

    def _show_error(self, message):
        if not self:
            return
        self.valid_packages.clear()
        self.ok_button.Disable()
        self.package_list_box.Clear()
        self.package_list_box.Append(message)

    def on_package_select(self, event):
        value = self.package_list_box.GetStringSelection()
        self.selected_package = value if value in self.valid_packages else None
        self.ok_button.Enable(self.selected_package is not None)
        self.alias_input.SetValue(self.selected_package or "")

    def on_ok(self, event):
        if self.selected_package not in self.valid_packages:
            return
        self.alias = self.alias_input.GetValue().strip()
        self.EndModal(wx.ID_OK)

    def get_selection(self):
        return self.alias, self.selected_package


class RemoteFilePickerDialog(wx.Dialog):
    """远程文件选择对话框 (优化：使用目录树)"""
    def __init__(self, parent, adb_controller, serial, package_name):
        super().__init__(parent, title=f"选择查看文件 - {package_name}", size=(650, 500))
        self.adb_controller = adb_controller
        self.serial = serial
        self.package_name = package_name
        self.selected_file = None
        self.all_files = []

        self.init_ui()

        # 初始加载
        wx.CallAfter(self.load_files)

    def init_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # 顶部工具栏：搜索和刷新
        top_sizer = wx.BoxSizer(wx.HORIZONTAL)
        top_sizer.Add(wx.StaticText(panel, label="过滤文件:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.search_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_ctrl.Bind(wx.EVT_TEXT, self.on_filter)
        top_sizer.Add(self.search_ctrl, 1, wx.EXPAND | wx.RIGHT, 10)

        self.refresh_btn = wx.Button(panel, label="刷新目录", size=(80, -1))
        self.refresh_btn.Bind(wx.EVT_BUTTON, lambda e: self.load_files())
        top_sizer.Add(self.refresh_btn, 0, wx.EXPAND)

        sizer.Add(top_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # 文件树控件
        self.file_tree = wx.TreeCtrl(panel, style=wx.TR_HAS_BUTTONS | wx.TR_SINGLE | wx.TR_HIDE_ROOT)
        self.file_tree.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self.on_item_activated)
        sizer.Add(self.file_tree, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # 说明文字
        tip = wx.StaticText(panel, label="提示: 展开目录并双击文件名，或选择后点击确认开始监控。")
        tip.SetForegroundColour(wx.Colour(128, 128, 128))
        sizer.Add(tip, 0, wx.ALL, 10)

        # 底部按钮
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.ok_btn = wx.Button(panel, wx.ID_OK, label="确认监控")
        self.ok_btn.Bind(wx.EVT_BUTTON, self.on_ok)
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label="取消")
        btn_sizer.Add(self.ok_btn, 0, wx.RIGHT, 10)
        btn_sizer.Add(cancel_btn, 0)

        sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        panel.SetSizer(sizer)

    def load_files(self):
        if not self:
            return
        self.ok_btn.Disable()
        self.refresh_btn.Disable()
        self.file_tree.DeleteAllItems()
        self.SetTitle(f"正在同步远程文件变动... - {self.package_name}")

        def do_load():
            try:
                files = self.adb_controller.list_remote_files(self.serial, self.package_name)
                wx.CallAfter(self._update_tree, files, None)
            except Exception as exc:
                wx.CallAfter(self._update_tree, [], str(exc))

        threading.Thread(target=do_load, daemon=True).start()

    def _update_tree(self, files, error=None):
        if not self:
            return
        self.all_files = files
        self.build_tree()
        self.ok_btn.Enable(bool(files))
        self.refresh_btn.Enable()
        self.SetTitle(f"选择查看文件 - {self.package_name}")
        if not files:
            self.file_tree.AppendItem(self.file_tree.GetRootItem(),
                                     error or "未找到文件，请尝试刷新或确认应用已启动")

    def build_tree(self):
        self.file_tree.DeleteAllItems()
        root = self.file_tree.AddRoot("Root")

        filter_text = self.search_ctrl.GetValue().lower()
        prefix = f"/storage/emulated/0/Android/data/{self.package_name}/"

        # 用于存储节点，避免重复创建
        nodes = {"": root}

        for f in sorted(self.all_files):
            if filter_text and filter_text not in f.lower():
                continue

            # 去掉前缀，处理相对路径
            rel_path = f.replace(prefix, "")
            parts = rel_path.split("/")

            current_path = ""
            for i, part in enumerate(parts):
                parent_path = current_path
                current_path = f"{current_path}/{part}" if current_path else part

                if current_path not in nodes:
                    parent_node = nodes[parent_path]
                    new_node = self.file_tree.AppendItem(parent_node, part)
                    nodes[current_path] = new_node

                    # 如果是最后一个部分（文件），关联原始完整路径
                    if i == len(parts) - 1:
                        self.file_tree.SetItemData(new_node, f)

        # 自动展开有内容的节点
        self.file_tree.ExpandAll()

    def on_filter(self, event):
        self.build_tree()

    def on_item_activated(self, event):
        self.on_ok(None)

    def on_ok(self, event):
        item = self.file_tree.GetSelection()
        if item.IsOk():
            full_path = self.file_tree.GetItemData(item)
            if full_path:  # 只有文件节点才有 Data
                self.selected_file = full_path
                self.EndModal(wx.ID_OK)
                return
            else:
                # 如果点的是目录，则切换展开状态
                if self.file_tree.IsExpanded(item):
                    self.file_tree.Collapse(item)
                else:
                    self.file_tree.Expand(item)
                return

        if not self.all_files:
            self.EndModal(wx.ID_CANCEL)
        else:
            wx.MessageBox("请从树中双击选择一个文件进行查看", "提示")

    def get_selected_file(self):
        return self.selected_file
