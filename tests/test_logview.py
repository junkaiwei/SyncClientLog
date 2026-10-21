import unittest
from types import SimpleNamespace

import re
import wx
import wx.stc as stc

from logcatch.logview import LogView


class LogViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.events = []
        self.window = wx.Frame(None, size=(900, 500))
        self.view = LogView(self.window, self.events.append)
        self.window.Show()
        self.app.Yield()

    def tearDown(self):
        self.window.Destroy()
        self.app.Yield()

    def test_trailing_newline_keeps_real_text_visible(self):
        for index in range(20):
            self.view.append_content(''.join(f'{index} log {i}\n' for i in range(100)),
                                     500000, True)
            self.assertTrue(self.view.is_at_bottom())
            first = self.view.DocLineFromVisible(self.view.GetFirstVisibleLine())
            self.assertTrue(self.view.GetLine(first).strip())
        self.app.Yield()
        self.assertEqual(self.events, [])

    def test_truncation_preserves_content_anchor_and_selection(self):
        text = ''.join(f'line {i:04d} sample text\n' for i in range(1000))
        self.view.append_content(text, 500000, True)
        self.view.SetFirstVisibleLine(700)
        start = self.view.PositionFromLine(705)
        self.view.SetAnchor(start)
        self.view.SetCurrentPos(start + 4)
        before = self.view.capture_view()
        removed = self.view.append_content('trim append\n' * 100, 20000, False)
        after = self.view.capture_view()
        self.assertGreater(removed, 0)
        self.assertEqual(after[0], before[0] - removed)
        self.assertEqual(after[3:], (before[3] - removed, before[4] - removed))
        self.assertEqual(self.view.GetSelectedText(), 'line')

    def test_wrapped_reading_position_survives_append(self):
        self.view.append_content(('中文长行 ' * 150 + '\n') * 50, 500000, True)
        self.view.SetFirstVisibleLine(40)
        before = self.view.capture_view()
        self.view.append_content('new line\n', 500000, False)
        self.assertEqual(self.view.capture_view()[:2], before[:2])

    def test_chinese_emoji_regex_and_indicator_offsets(self):
        text = '开头 中文😀\n下一行 中文😀\n'
        self.view.append_content(text, 500000, True)
        spans = self.view.highlight_matches(re.compile('中文😀'))
        self.assertEqual(len(spans), 2)
        for start, end in spans:
            self.assertEqual(self.view.GetTextRange(start, end), '中文😀')
            self.assertTrue(self.view.IndicatorValueAt(self.view.MATCH, start))
        self.view.mark_current_match(spans[1])
        self.assertTrue(self.view.IndicatorValueAt(self.view.CURRENT_MATCH, spans[1][0]))
        self.view.clear_highlights()
        self.assertFalse(self.view.IndicatorValueAt(self.view.MATCH, spans[0][0]))

    def test_snapshot_size_counts_characters_not_bytes(self):
        text = '中😀' * 100
        self.view.replace_content(text, 40, True)
        self.assertEqual(self.view.GetText(), text[-40:])
        self.assertEqual(self.view.GetTextLength(), len(text[-40:].encode('utf-8')))
        self.assertTrue(self.view.GetReadOnly())
        self.view.SetAnchor(0)
        self.view.SetCurrentPos(self.view.GetTextLength())
        self.view.CmdKeyExecute(stc.STC_CMD_CLEAR)
        self.assertEqual(self.view.GetText(), text[-40:])

    def test_user_scroll_notified_but_programmatic_tail_is_not(self):
        self.view.append_content('sample\n' * 500, 500000, True)
        self.app.Yield()
        self.view.LineScroll(0, -50)
        self.view.Update()
        self.app.Yield()
        self.assertEqual(self.events, [False])
        self.events.clear()
        self.view.scroll_to_tail()
        self.app.Yield()
        self.assertEqual(self.events, [])
        self.assertTrue(self.view.is_at_bottom())

    def wheel(self, rotation, axis=wx.MOUSE_WHEEL_VERTICAL):
        self.view._on_mouse_wheel(SimpleNamespace(
            GetWheelAxis=lambda: axis, GetWheelRotation=lambda: rotation, Skip=lambda: None))
        self.app.Yield()

    def test_upward_wheel_disables_even_when_last_line_remains_visible(self):
        self.view.append_content('short log\n', 500000, True)
        self.assertTrue(self.view.is_at_bottom())
        self.wheel(120)
        self.assertTrue(self.view.is_at_bottom())
        self.assertEqual(self.events, [False])

    def test_downward_scroll_in_middle_does_not_change_switch(self):
        self.view.append_content('sample\n' * 500, 500000, True)
        self.view.SetFirstVisibleLine(100)
        self.view.Update()
        self.app.Yield()
        self.events.clear()
        self.view.LineScroll(0, 10)
        self.view.Update()
        self.app.Yield()
        self.assertFalse(self.view.is_at_bottom())
        self.assertEqual(self.events, [])
        self.wheel(-120)
        self.assertEqual(self.events, [])

    def test_downward_wheel_at_bottom_enables_without_view_movement(self):
        self.view.append_content('short log\n', 500000, True)
        self.wheel(-120)
        self.assertEqual(self.events, [True])

    def test_horizontal_and_zero_wheel_do_not_change_switch(self):
        self.view.append_content('short log\n', 500000, True)
        self.wheel(120, wx.MOUSE_WHEEL_HORIZONTAL)
        self.wheel(-120, wx.MOUSE_WHEEL_HORIZONTAL)
        self.wheel(0)
        self.assertEqual(self.events, [])

    def test_upward_wheel_during_programmatic_update_is_ignored(self):
        with self.view._change():
            self.wheel(120)
        self.assertEqual(self.events, [])

    def test_keyboard_navigation_and_return_to_bottom(self):
        self.view.append_content('sample\n' * 500, 500000, True)
        self.view.CmdKeyExecute(stc.STC_CMD_PAGEUP)
        self.view.Update()
        self.app.Yield()
        self.assertEqual(self.events[-1], False)
        self.view.CmdKeyExecute(stc.STC_CMD_DOCUMENTEND)
        self.view.Update()
        self.app.Yield()
        self.assertEqual(self.events[-1], True)

    def test_snapshot_selection_never_splits_utf8_character(self):
        self.view.replace_content('abcdef', 100, False)
        self.view.SetAnchor(2)
        self.view.SetCurrentPos(4)
        self.view.replace_content('中文😀', 100, False)
        raw = self.view.GetText().encode('utf-8')
        raw[:self.view.GetAnchor()].decode('utf-8')
        raw[:self.view.GetCurrentPos()].decode('utf-8')

    def test_wrapped_anchor_survives_prefix_truncation(self):
        text = ''.join(f'中文 {index} ' + 'long text ' * 50 + '\n' for index in range(200))
        self.view.append_content(text, 500000, True)
        self.view.SetFirstVisibleLine(self.view.VisibleFromDocLine(150) + 2)
        before = self.view.capture_view()
        removed = self.view.append_content('extra\n' * 10, 100000, False)
        after = self.view.capture_view()
        self.assertGreater(removed, 0)
        self.assertEqual(after[:2], (before[0] - removed, before[1]))


if __name__ == '__main__':
    unittest.main()
