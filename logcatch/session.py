"""One owner for the session file, including snapshot and export operations."""
from pathlib import Path
import shutil


class SessionLog:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def start(self):
        self.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('w', encoding='utf-8')

    def append(self, content):
        if self.file is not None:
            self.file.write(content)

    def snapshot(self, content):
        if self.file is not None:
            self.file.seek(0)
            self.file.truncate()
            self.file.write(content)

    def export(self, destination):
        if self.file is not None:
            self.file.flush()
        if not self.path.exists():
            raise FileNotFoundError('没有日志内容可导出')
        shutil.copy2(self.path, destination)

    def close(self):
        if self.file is not None:
            file, self.file = self.file, None
            file.close()

    def discard(self):
        self.close()
        self.path.unlink(missing_ok=True)
