"""Log display with explicit UTF-8 positions and document-based view anchors."""
from collections import deque
from contextlib import contextmanager

import wx
import wx.stc as stc


class LogView(stc.StyledTextCtrl):
    MATCH = 8
    CURRENT_MATCH = 9

    def __init__(self, parent, on_user_scroll=None):
        super().__init__(parent)
        self.on_user_scroll = on_user_scroll
        self._changing = 0
        self._known_first_line = 0
        self._char_count = 0
        self.SetCodePage(stc.STC_CP_UTF8)
        self.SetEOLMode(stc.STC_EOL_LF)
        self.StyleSetFont(stc.STC_STYLE_DEFAULT,
                          wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                                  wx.FONTWEIGHT_NORMAL))
        self.StyleSetForeground(stc.STC_STYLE_DEFAULT, wx.Colour('black'))
        self.StyleSetBackground(stc.STC_STYLE_DEFAULT, wx.Colour('white'))
        self.StyleClearAll()
        for margin in range(5):
            self.SetMarginWidth(margin, 0)
        self.SetWrapMode(stc.STC_WRAP_WORD)
        self.SetEndAtLastLine(True)
        self.SetUndoCollection(False)
        self.SetReadOnly(True)
        for indicator, colour in [(self.MATCH, 'yellow'), (self.CURRENT_MATCH, 'blue')]:
            self.IndicatorSetStyle(indicator, stc.STC_INDIC_ROUNDBOX)
            self.IndicatorSetForeground(indicator, wx.Colour(colour))
            self.IndicatorSetAlpha(indicator, 100)
            self.IndicatorSetOutlineAlpha(indicator, 180)
            self.IndicatorSetUnder(indicator, True)
        self.Bind(stc.EVT_STC_UPDATEUI, self._on_update_ui)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_mouse_wheel)
        self.Bind(wx.EVT_KEY_DOWN, self._on_navigation_key)

    @contextmanager
    def _change(self, editable=False):
        self._changing += 1
        if editable:
            self.SetReadOnly(False)
        try:
            yield
        finally:
            if editable:
                self.SetReadOnly(True)
            self.Update()
            self._known_first_line = self.GetFirstVisibleLine()
            self._changing -= 1

    def _on_update_ui(self, event):
        if not self._changing and event.GetUpdated() & stc.STC_UPDATE_V_SCROLL:
            first = self.GetFirstVisibleLine()
            if first != self._known_first_line:
                direction = first - self._known_first_line
                self._known_first_line = first
                self._apply_scroll_direction(direction)
        event.Skip()

    def _apply_scroll_direction(self, direction):
        if self._changing or not self.on_user_scroll:
            return
        if direction < 0:
            self.on_user_scroll(False)
        elif direction > 0 and self.is_at_bottom():
            self.on_user_scroll(True)

    def _after_downward_input(self):
        if self:
            self._apply_scroll_direction(1)

    def _on_mouse_wheel(self, event):
        if event.GetWheelAxis() == wx.MOUSE_WHEEL_VERTICAL:
            rotation = event.GetWheelRotation()
            if rotation > 0:
                # Upward intent also disables follow when the viewport cannot move.
                self._apply_scroll_direction(-1)
            elif rotation < 0 and not self._changing:
                # Let the native control scroll before checking the last line.
                wx.CallAfter(self._after_downward_input)
        event.Skip()

    def _on_navigation_key(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_UP, wx.WXK_PAGEUP) or (key == wx.WXK_HOME and event.ControlDown()):
            self._apply_scroll_direction(-1)
        elif (key in (wx.WXK_DOWN, wx.WXK_PAGEDOWN) or
              (key == wx.WXK_END and event.ControlDown())) and not self._changing:
            wx.CallAfter(self._after_downward_input)
        event.Skip()

    def last_display_line(self):
        line = max(0, self.GetLineCount() - 1)
        return self.VisibleFromDocLine(line) + max(1, self.WrapCount(line)) - 1

    def is_at_bottom(self):
        return self.GetFirstVisibleLine() + max(1, self.LinesOnScreen()) > self.last_display_line()

    def _scroll_to_tail(self):
        first = max(0, self.last_display_line() - max(1, self.LinesOnScreen()) + 1)
        self.SetFirstVisibleLine(first)

    def scroll_to_tail(self):
        with self._change():
            self._scroll_to_tail()

    def capture_view(self):
        first = self.GetFirstVisibleLine()
        line = self.DocLineFromVisible(first)
        return (self.PositionFromLine(line), first - self.VisibleFromDocLine(line),
                self.GetXOffset(), self.GetAnchor(), self.GetCurrentPos())

    def _restore_view(self, view, removed_bytes=0):
        position, subline, horizontal, anchor, caret = view
        length = self.GetTextLength()
        position = min(length, max(0, position - removed_bytes))
        line = self.LineFromPosition(position)
        if view[0] < removed_bytes:
            subline = 0
        def valid_position(value):
            value = min(length, max(0, value - removed_bytes))
            # A replacement snapshot can have different UTF-8 byte boundaries.
            return self.PositionBefore(self.PositionAfter(value)) if 0 < value < length else value
        self.SetAnchor(valid_position(anchor))
        self.SetCurrentPos(valid_position(caret))
        self.SetFirstVisibleLine(self.VisibleFromDocLine(line) +
                                 min(subline, max(0, self.WrapCount(line) - 1)))
        self.SetXOffset(horizontal)

    @staticmethod
    def _cut_position(text, minimum):
        boundary = text.find('\n', minimum)
        # A single giant line still needs a Unicode-safe size bound.
        return boundary + 1 if 0 <= boundary < len(text) - 1 else minimum

    def append_content(self, content, max_chars, follow):
        view = self.capture_view()
        removed_bytes = 0
        with self._change(editable=True):
            self.AppendText(content)
            self._char_count += len(content)
            if self._char_count > max_chars:
                text = self.GetText()
                cut = self._cut_position(text, len(text) - max_chars // 2)
                removed_bytes = len(text[:cut].encode('utf-8'))
                self.DeleteRange(0, removed_bytes)
                self._char_count -= cut
            # Wrapping must be laid out before converting document/display lines.
            self.Update()
            if follow:
                self._scroll_to_tail()
            else:
                self._restore_view(view, removed_bytes)
        return removed_bytes

    def replace_content(self, content, max_chars, follow):
        view = self.capture_view()
        cut = self._cut_position(content, len(content) - max_chars) if len(content) > max_chars else 0
        with self._change(editable=True):
            self.SetText(content[cut:])
            self._char_count = len(content) - cut
            self.Update()
            if follow:
                self._scroll_to_tail()
            else:
                self._restore_view(view)

    def clear_content(self):
        with self._change(editable=True):
            self.ClearAll()
            self._char_count = 0

    def clear_highlights(self):
        for indicator in (self.MATCH, self.CURRENT_MATCH):
            self.SetIndicatorCurrent(indicator)
            self.IndicatorClearRange(0, self.GetTextLength())

    def highlight_matches(self, pattern, limit=1000):
        """Convert sorted Python character spans to UTF-8 byte spans in one pass."""
        self.clear_highlights()
        if pattern is None:
            return []
        text = self.GetText()
        results = deque(maxlen=limit)
        previous_char = 0
        previous_byte = 0
        self.SetIndicatorCurrent(self.MATCH)
        for match in pattern.finditer(text):
            start = previous_byte + len(text[previous_char:match.start()].encode('utf-8'))
            end = start + len(match.group().encode('utf-8'))
            self.IndicatorFillRange(start, end - start)
            results.append((start, end))
            previous_char, previous_byte = match.end(), end
        return list(results)

    def mark_current_match(self, span):
        self.SetIndicatorCurrent(self.CURRENT_MATCH)
        self.IndicatorClearRange(0, self.GetTextLength())
        if span is not None:
            self.IndicatorFillRange(span[0], span[1] - span[0])

    def reveal_match(self, start, end):
        with self._change():
            self.SetAnchor(start)
            self.SetCurrentPos(end)
            first = self.VisibleFromDocLine(self.LineFromPosition(start))
            self.SetFirstVisibleLine(max(0, first - max(1, self.LinesOnScreen()) // 2))
