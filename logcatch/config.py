import os
import re
import functools

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
