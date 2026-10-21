"""Exercise real wx controls without ADB devices or user configuration files."""
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import wx

from logcatch.frame import CatchLogFrame
from logcatch.dialogs import PackageListDialog


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_directory = os.getcwd()
        os.chdir(self.directory.name)
        self.messages = patch('wx.MessageBox', return_value=wx.OK)
        self.messages.start()
        with patch.object(CatchLogFrame, 'refresh_devices'):
            self.frame = CatchLogFrame()
        self.frame.Show()
        self.app.Yield()
        self.frame._start_session()
    def tearDown(self):
        self.frame.closing = True
        self.frame.session.discard()
        self.frame.search_timer.Stop()
        self.frame.Destroy()
        self.app.Yield()
        self.messages.stop()
        os.chdir(self.previous_directory)
        self.directory.cleanup()

    def test_disabled_autoscroll_still_appends_and_exports(self):
        self.frame.autoScroll.SetValue(False)
        self.frame.update_log_text(['first\n'])
        self.frame.update_log_text(['second\n'])
        self.assertEqual(self.frame.logCtrl.GetValue(), 'first\nsecond\n')
        self.frame.autoScroll.SetValue(True)
        self.frame.on_autoscroll_checkbox_manual_toggle(wx.CommandEvent())
        self.assertEqual(self.frame.logCtrl.GetValue(), 'first\nsecond\n')
        destination = Path(self.directory.name) / 'full.txt'
        self.frame.session.export(destination)
        self.assertEqual(destination.read_text(encoding='utf-8'), 'first\nsecond\n')

    def test_reading_position_is_preserved(self):
        self.frame.update_log_text([f'line {i}\n' for i in range(300)])
        self.frame.autoScroll.SetValue(False)
        self.frame.logCtrl.SetSelection(5, 10)
        self.frame.logCtrl.SetFirstVisibleLine(0)
        self.app.Yield()
        previous = self.frame.logCtrl.GetFirstVisibleLine()
        self.frame.update_log_text(['new line\n'])
        self.assertEqual(self.frame.logCtrl.GetSelection(), (5, 10))
        self.assertEqual(self.frame.logCtrl.GetFirstVisibleLine(), previous)
        self.assertTrue(self.frame.logCtrl.GetValue().endswith('new line\n'))

    def test_trim_rebuilds_search_but_keeps_complete_export(self):
        self.frame.autoScroll.SetValue(False)
        self.frame.update_log_text(['old hit\n'])
        self.frame.searchCtrl.ChangeValue('hit')
        self.frame.on_execute_search(None)
        with patch('logcatch.frame.MAX_CHARS', 40):
            self.frame.update_log_text(['filler ' * 8 + '\nhit\n'])
        content = self.frame.logCtrl.GetValue()
        self.assertLessEqual(len(content), 40)
        for start, end in self.frame.search_manager.results:
            self.assertEqual(self.frame.logCtrl.GetRange(start, end), 'hit')
        self.assertEqual(len(self.frame.search_manager.results), 1)
        destination = Path(self.directory.name) / 'full.txt'
        self.frame.session.export(destination)
        self.assertTrue(destination.read_text(encoding='utf-8').startswith('old hit\n'))

    def test_first_search_and_clear(self):
        self.frame.update_log_text(['hit one hit two\n'])
        self.frame.searchCtrl.ChangeValue('hit')
        self.frame.on_execute_search(None)
        self.assertEqual(self.frame.search_manager.current_index, 0)
        self.assertEqual(self.frame.search_manager.results[0], (0, 3))
        self.frame.searchCtrl.ChangeValue('')
        self.frame.on_execute_search(None)
        self.assertFalse(self.frame.search_manager.has_results())
        self.assertFalse(self.frame.nextBtn.IsEnabled())
        self.assertFalse(self.frame.logCtrl.IndicatorValueAt(self.frame.logCtrl.MATCH, 0))

    def test_search_navigation_pauses_follow_until_manual_resume(self):
        self.frame.update_log_text([f'中文{i} 😀 sample\n' for i in range(300)])
        self.frame.searchCtrl.ChangeValue('中文100')
        self.frame.on_execute_search(None)
        self.assertFalse(self.frame.autoScroll.IsChecked())
        before = self.frame.logCtrl.capture_view()
        self.frame.update_log_text(['new line\n'])
        self.assertEqual(self.frame.logCtrl.capture_view()[:2], before[:2])
        self.frame.autoScroll.SetValue(True)
        self.frame.on_autoscroll_checkbox_manual_toggle(wx.CommandEvent())
        self.assertTrue(self.frame.logCtrl.is_at_bottom())

    def test_follow_unchecked_at_bottom_is_not_reenabled_by_append(self):
        self.frame.update_log_text(['sample\n' * 300])
        self.frame.autoScroll.SetValue(False)
        self.frame.update_log_text(['new\n'])
        self.app.Yield()
        self.assertFalse(self.frame.autoScroll.IsChecked())

    def test_scroll_direction_drives_follow_switch(self):
        self.frame.update_log_text(['sample\n' * 500])
        self.frame.logCtrl.LineScroll(0, -100)
        self.frame.logCtrl.Update()
        self.app.Yield()
        self.assertFalse(self.frame.autoScroll.IsChecked())
        self.frame.logCtrl.LineScroll(0, 10)
        self.frame.logCtrl.Update()
        self.app.Yield()
        self.assertFalse(self.frame.autoScroll.IsChecked())
        self.frame.autoScroll.SetValue(True)
        self.frame.logCtrl.LineScroll(0, 10)
        self.frame.logCtrl.Update()
        self.app.Yield()
        self.assertFalse(self.frame.logCtrl.is_at_bottom())
        self.assertTrue(self.frame.autoScroll.IsChecked())
        self.frame.autoScroll.SetValue(False)
        self.frame.logCtrl.LineScroll(0, 1000)
        self.frame.logCtrl.Update()
        self.app.Yield()
        self.assertTrue(self.frame.autoScroll.IsChecked())

    def test_short_log_wheel_controls_switch_without_scrolling(self):
        self.frame.update_log_text(['short log\n'])
        def wheel(rotation):
            self.frame.logCtrl._on_mouse_wheel(SimpleNamespace(
                GetWheelAxis=lambda: wx.MOUSE_WHEEL_VERTICAL,
                GetWheelRotation=lambda: rotation, Skip=lambda: None))
            self.app.Yield()
        wheel(120)
        self.assertFalse(self.frame.autoScroll.IsChecked())
        self.assertTrue(self.frame.logCtrl.is_at_bottom())
        self.frame.update_log_text(['still short\n'])
        self.app.Yield()
        self.assertFalse(self.frame.autoScroll.IsChecked())
        wheel(-120)
        self.assertTrue(self.frame.autoScroll.IsChecked())

    def test_regex_spanning_batches_and_utf8_positions(self):
        self.frame.regexCheck.SetValue(True)
        self.frame.searchCtrl.ChangeValue('中文😀\\n继续')
        self.frame.on_execute_search(None)
        self.frame.update_log_text(['前缀 中文😀\n'])
        self.frame.update_log_text(['继续\n'])
        self.assertEqual(len(self.frame.search_manager.results), 1)
        start, end = self.frame.search_manager.results[0]
        self.assertEqual(self.frame.logCtrl.GetTextRange(start, end), '中文😀\n继续')

    def test_next_previous_current_indicator_and_copy_range(self):
        self.frame.update_log_text(['中文😀 one 中文😀 two\n'])
        self.frame.searchCtrl.ChangeValue('中文😀')
        self.frame.regexCheck.SetValue(True)
        self.frame.on_execute_search(None)
        self.frame.on_find_next(None)
        self.assertEqual(self.frame.search_manager.current_index, 1)
        self.assertEqual(self.frame.logCtrl.GetSelectedText(), '中文😀')
        self.frame.on_find_prev(None)
        start, _ = self.frame.search_manager.results[0]
        self.assertTrue(self.frame.logCtrl.IndicatorValueAt(
            self.frame.logCtrl.CURRENT_MATCH, start))

    def test_invalid_regex_clears_old_matches(self):
        self.frame.update_log_text(['hit\n'])
        self.frame.searchCtrl.ChangeValue('hit')
        self.frame.on_execute_search(None)
        self.frame.regexCheck.SetValue(True)
        self.frame.searchCtrl.ChangeValue('[')
        self.frame.on_execute_search(None)
        self.assertFalse(self.frame.search_manager.has_results())
        self.assertFalse(self.frame.nextBtn.IsEnabled())
        self.assertFalse(self.frame.logCtrl.IndicatorValueAt(self.frame.logCtrl.MATCH, 0))

    def test_stale_and_closed_callbacks_are_ignored(self):
        delivered = []
        current = self.frame.session_id
        self.frame._deliver_session(current - 1, delivered.append, ('stale',))
        self.frame._deliver_session(current, delivered.append, ('current',))
        self.frame.closing = True
        self.frame._deliver_session(current, delivered.append, ('closed',))
        self.assertEqual(delivered, ['current'])

    def test_snapshot_and_failed_refresh(self):
        self.frame.refresh_file_content('snapshot\n')
        self.frame.file_refresh_error('offline')
        self.assertEqual(self.frame.logCtrl.GetValue(), 'snapshot\n')
        self.frame.session.export(Path(self.directory.name) / 'snapshot.txt')
        self.assertEqual((Path(self.directory.name) / 'snapshot.txt').read_text(
            encoding='utf-8'), 'snapshot\n')

    def test_snapshot_limit_keeps_complete_export(self):
        content = 'long line ' * 100
        with patch('logcatch.frame.MAX_CHARS', 40):
            self.frame.refresh_file_content(content)
        self.assertEqual(self.frame.logCtrl.GetValue(), content[-40:])
        destination = Path(self.directory.name) / 'snapshot.txt'
        self.frame.session.export(destination)
        self.assertEqual(destination.read_text(encoding='utf-8'), content)

    def test_queued_callbacks_arrive_before_session_closes(self):
        receive = self.frame._session_callback(self.frame.update_log_text)
        stop = self.frame._session_callback(self.frame.on_logging_stopped)
        receive(['final batch\n'])
        stop()
        self.app.Yield()
        self.assertFalse(self.frame.is_logging)
        self.assertIsNone(self.frame.session.file)
        self.assertEqual(Path(self.frame.session_log_path).read_text(encoding='utf-8'),
                         'final batch\n')

    def test_unavailable_device_rejected(self):
        self.frame.deviceChoice.Append('offline', 'serial\toffline')
        self.frame.deviceChoice.SetSelection(0)
        self.assertIsNone(self.frame._selected_serial())

    def test_close_error_does_not_leave_logging_active(self):
        with patch.object(self.frame.session, 'close', side_effect=OSError('disk error')):
            self.frame.on_logging_stopped()
        self.assertFalse(self.frame.is_logging)
        self.assertTrue(self.frame.startButton.IsEnabled())
        self.assertIn('disk error', self.frame.GetStatusBar().GetStatusText())

    def test_error_placeholder_cannot_be_saved_as_package(self):
        with patch.object(PackageListDialog, 'load_packages_in_thread'):
            dialog = PackageListDialog(self.frame, 'adb', 'serial')
        try:
            dialog._show_error('permission denied')
            dialog.package_list_box.SetSelection(0)
            dialog.on_package_select(None)
            self.assertIsNone(dialog.selected_package)
            self.assertFalse(dialog.ok_button.IsEnabled())
        finally:
            dialog.Destroy()


if __name__ == '__main__':
    unittest.main()
