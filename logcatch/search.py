from collections import deque

class SearchResultManager:
    """搜索结果管理；匹配范围使用 STC 的 UTF-8 字节位置。"""

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
