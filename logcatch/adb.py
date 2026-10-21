import os
import re
import shlex
import subprocess

class AdbController:
    """封装所有ADB命令的执行逻辑"""

    def __init__(self, adb_path='adb'):
        self.adb_path = adb_path

    def _run_command(self, cmd, timeout=10, encoding='utf-8', **kwargs):
        # 统一执行ADB命令的私有方法
        return subprocess.run(
            [self.adb_path] + cmd,
            capture_output=True, text=True, timeout=timeout,
            encoding=encoding, errors='replace',
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

    def connect_device(self, address):
        """主动连接网络设备，未指定端口时使用 ADB 默认端口。"""
        address = address.strip()
        match = re.fullmatch(r'([a-zA-Z0-9_.-]+)(?::([0-9]{1,5}))?', address)
        if not match:
            return False, "请输入 IP 或主机名，可附加端口，例如 127.0.0.1:5555", address
        host, port = match.groups()
        port = int(port or 5555)
        if not 1 <= port <= 65535:
            return False, "端口必须在 1 到 65535 之间", address
        address = f"{host}:{port}"
        try:
            result = self._run_command(['connect', address], timeout=10)
            message = '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
            # adb connect 失败时也可能返回 0，必须同时检查连接结果。
            success = result.returncode == 0 and any(
                line.lower().startswith(('connected to ', 'already connected to '))
                for line in result.stdout.splitlines()
            )
            return success, message or "ADB 未返回连接结果", address
        except subprocess.TimeoutExpired:
            return False, "连接设备超时，请检查地址、端口和网络", address
        except Exception as e:
            return False, f"连接设备失败: {e}", address

    def clear_logcat(self, serial):
        """清空设备日志"""
        try:
            result = self._run_command(['-s', serial, 'logcat', '-c'], timeout=3)
            if result.returncode != 0:
                return False, result.stderr.strip() or '清空日志失败'
            return True, ""
        except subprocess.TimeoutExpired:
            return False, "清空日志超时"
        except Exception as e:
            return False, f"清空日志失败: {e}"

    def get_pid(self, serial, package_name):
        """获取应用的PID"""
        try:
            result = self._run_command(['-s', serial, 'shell', f'pidof {shlex.quote(package_name)}'],
                                       timeout=5, check=True)
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

    def list_remote_files(self, serial, package_name):
        """递归列出 Android/data 下的子文件 (包含 files, cache 等)"""
        base_path = f"/storage/emulated/0/Android/data/{package_name}"
        result = self._run_command(
            ['-s', serial, 'shell', f'find {shlex.quote(base_path)} -maxdepth 4 -type f'],
            timeout=15)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or '无法列出远程文件')
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def get_file_content_command(self, serial, remote_path, lines=1000):
        """构建用于拉取文件最新内容的 shell 命令"""
        return ['-s', serial, 'shell', f'tail -n {int(lines)} {shlex.quote(remote_path)}']

    def read_file(self, serial, remote_path, encoding='utf-8'):
        result = self._run_command(self.get_file_content_command(serial, remote_path),
                                   timeout=5, encoding=encoding)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or '读取文件失败')
        return result.stdout

    def pull_log(self, serial, package_name, dest_path, remote_path=None):
        """从设备拉取日志文件"""
        src = remote_path if remote_path else f"/storage/emulated/0/Android/data/{package_name}/files/outLog.txt"
        try:
            self._run_command(["-s", serial, "pull", src, dest_path], timeout=30, check=True)
            return True, f"日志导出成功:\n{os.path.abspath(dest_path)}"
        except subprocess.CalledProcessError as e:
            return False, f"导出失败，文件可能不存在或无权限:\n{e.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return False, "导出超时，请检查文件大小和设备连接"
        except Exception as e:
            return False, f"导出失败: {e}"
