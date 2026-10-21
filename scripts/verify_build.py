"""Check a standalone Windows EXE without external project metadata."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from logcatch.version import get_version


def verify(executable):
    if sys.platform != 'win32':
        raise RuntimeError('此检查需要 Windows')
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    expected = f'IKUN LogCatch V{get_version()}'
    with tempfile.TemporaryDirectory(prefix='ikun-build-') as directory:
        copy = Path(directory) / 'ikunLogCatch.exe'
        shutil.copy2(Path(executable).resolve(), copy)
        process = subprocess.Popen([str(copy)], cwd=directory,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 30
            handle = None
            while time.monotonic() < deadline and process.poll() is None:
                matches = []
                @callback_type
                def inspect(hwnd, _):
                    title = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, title, len(title))
                    if title.value == expected:
                        # Onefile uses a child process; only accept our isolated copy.
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if process_path(pid.value) == str(copy):
                            matches.append(hwnd)
                    return True
                user32.EnumWindows(inspect, 0)
                if matches:
                    handle = matches[0]
                    break
                time.sleep(0.1)
            if handle is None:
                raise RuntimeError(f'未找到预期窗口 {expected!r}，退出码 {process.poll()}')
            user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE
            if process.wait(timeout=15) != 0:
                raise RuntimeError('EXE 退出异常')
            print(f'独立 EXE 启动并正常关闭: {expected}；无外部 pyproject.toml')
        finally:
            if process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                process.wait(timeout=10)


def process_path(pid):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        name = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(name))
        if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
            return name.value
    finally:
        kernel32.CloseHandle(handle)
    return None


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('用法: python scripts/verify_build.py <EXE 路径>')
    verify(sys.argv[1])
