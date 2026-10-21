"""Background readers; callbacks are dispatched by the window, never here."""
import os
import queue
import subprocess
import threading
import time

from .config import BUFFER_SIZE, FLUSH_INTERVAL


class LogThread(threading.Thread):
    def __init__(self, cmd, on_lines, on_error, on_stopped, encoding='utf-8'):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.encoding = encoding
        self.on_lines = on_lines
        self.on_error = on_error
        self.on_stopped = on_stopped
        self.stop_event = threading.Event()
        self.proc = None

    def run(self):
        buffer = []
        reader = None
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding=self.encoding, errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            # Reading a pipe blocks on quiet streams, so the batch timer must run
            # independently of readline. Bound the queue for busy devices.
            messages = queue.Queue(maxsize=BUFFER_SIZE * 10)

            def enqueue(message):
                while True:
                    try:
                        messages.put(message, timeout=0.1)
                        return
                    except queue.Full:
                        continue

            def read_pipe():
                try:
                    for line in self.proc.stdout:
                        enqueue(('line', line))
                except Exception as exc:
                    enqueue(('error', str(exc)))
                finally:
                    enqueue(('eof', None))

            reader = threading.Thread(target=read_pipe, daemon=True)
            reader.start()
            if self.stop_event.is_set() and self.proc.poll() is None:
                self.proc.terminate()
            last_flush = time.monotonic()
            while True:
                try:
                    kind, value = messages.get(timeout=0.05)
                except queue.Empty:
                    kind, value = None, None
                if kind == 'line':
                    buffer.append(value)
                elif kind == 'error':
                    self.on_error(value)
                if buffer and (len(buffer) >= BUFFER_SIZE or
                               time.monotonic() - last_flush >= FLUSH_INTERVAL):
                    self.on_lines(buffer.copy())
                    buffer.clear()
                    last_flush = time.monotonic()
                if kind == 'eof' or (self.stop_event.is_set() and
                                     not reader.is_alive() and messages.empty()):
                    break
            code = self.proc.wait(timeout=3)
            if code and not self.stop_event.is_set():
                self.on_error(f'日志命令退出，状态码: {code}')
        except Exception as exc:
            self.on_error(str(exc))
        finally:
            if self.proc:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait()
                if reader:
                    reader.join(timeout=1)
                self.proc.stdout.close()
            if buffer:
                self.on_lines(buffer.copy())
            self.on_stopped()

    def stop(self):
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class FilePollingThread(threading.Thread):
    """Fetch a snapshot initially and on demand, not on a periodic timer."""
    def __init__(self, adb_controller, serial, remote_path,
                 on_content, on_error, on_stopped, encoding='utf-8'):
        super().__init__(daemon=True)
        self.adb_controller = adb_controller
        self.serial = serial
        self.remote_path = remote_path
        self.encoding = encoding
        self.on_content = on_content
        self.on_error = on_error
        self.on_stopped = on_stopped
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()

    def run(self):
        try:
            self.refresh_event.set()
            while not self.stop_event.is_set():
                self.refresh_event.wait()
                if self.stop_event.is_set():
                    break
                # Clear before I/O so requests during a fetch become one next fetch.
                self.refresh_event.clear()
                self._do_single_refresh()
        finally:
            self.on_stopped()

    def _do_single_refresh(self):
        try:
            content = self.adb_controller.read_file(
                self.serial, self.remote_path, self.encoding)
            if not self.stop_event.is_set():
                self.on_content(content)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.on_error(f'文件刷新失败: {exc}')

    def trigger_refresh(self):
        self.refresh_event.set()

    def stop(self):
        self.stop_event.set()
        self.refresh_event.set()
