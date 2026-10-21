from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from logcatch.adb import AdbController
from logcatch.session import SessionLog
from logcatch.version import get_version, metadata_path, version_from_tag
from logcatch.workers import FilePollingThread, LogThread
from scripts.prepare_version import prepare_version


class VersionTests(unittest.TestCase):
    def test_tag_versions(self):
        for tag, version in [('v3.1.0', '3.1.0'), ('3.1.0', '3.1.0'),
                             ('V4.0.1', '4.0.1'), ('V4.0.1rc1', '4.0.1rc1'),
                             ('v3.2.0rc1', '3.2.0rc1'), ('3.2.0b2', '3.2.0b2')]:
            self.assertEqual(version_from_tag(tag), version)
        for tag in ['v', 'V', '', 'release-3.1.0', '3.1', '3.1.0;echo', '3.01.0']:
            with self.assertRaises(ValueError):
                version_from_tag(tag)

    def test_prepare_only_updates_project(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'pyproject.toml'
            file.write_text('[project]\nname="example"\nversion = "3.1.0"\n'
                            '[tool.example]\nversion = "keep"\n', encoding='utf-8')
            self.assertEqual(prepare_version('v4.0.0rc1', file), '4.0.0rc1')
            self.assertEqual(get_version(file), '4.0.0rc1')
            self.assertEqual(prepare_version('V4.0.1', file), '4.0.1')
            self.assertEqual(get_version(file), '4.0.1')
            self.assertIn('version = "keep"', file.read_text(encoding='utf-8'))
            before = file.read_bytes()
            with self.assertRaises(ValueError):
                prepare_version('invalid', file)
            self.assertEqual(before, file.read_bytes())

    def test_invalid_or_missing_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'pyproject.toml'
            with self.assertRaises(RuntimeError):
                get_version(file)
            file.write_text('[project]\nversion="bad"', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                get_version(file)

    def test_frozen_resource_path(self):
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, '_MEIPASS', 'C:/bundle', create=True):
            self.assertEqual(metadata_path(), Path('C:/bundle/pyproject.toml'))


class AdbTests(unittest.TestCase):
    def test_zero_exit_connect_failure(self):
        adb = AdbController()
        with patch.object(adb, '_run_command', return_value=subprocess.CompletedProcess(
                [], 0, 'failed to connect', '')):
            self.assertFalse(adb.connect_device('localhost')[0])
        with patch.object(adb, '_run_command', return_value=subprocess.CompletedProcess(
                [], 0, 'connected to localhost:5555', '')):
            result = adb.connect_device('localhost')
            self.assertTrue(result[0])
            self.assertEqual(result[2], 'localhost:5555')

    def test_clear_failure(self):
        adb = AdbController()
        with patch.object(adb, '_run_command', return_value=subprocess.CompletedProcess(
                [], 1, '', 'offline')):
            self.assertEqual(adb.clear_logcat('device'), (False, 'offline'))

    def test_gbk_decoding_and_quoted_path(self):
        adb = AdbController(sys.executable)
        result = adb._run_command(['-c', "import sys; sys.stdout.buffer.write('中文'.encode('gbk'))"],
                                  encoding='gbk')
        self.assertEqual(result.stdout, '中文')
        with patch.object(adb, '_run_command', return_value=subprocess.CompletedProcess(
                [], 0, '中文', '')) as run:
            self.assertEqual(adb.read_file('device', '/data/a b.txt', 'gbk'), '中文')
            self.assertEqual(run.call_args.kwargs['encoding'], 'gbk')
            self.assertIn("'/data/a b.txt'", run.call_args.args[0][-1])

    def test_remote_errors_are_not_empty_files(self):
        adb = AdbController()
        with patch.object(adb, '_run_command', return_value=subprocess.CompletedProcess(
                [], 1, '', 'permission denied')):
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                adb.read_file('device', '/missing')
            with self.assertRaisesRegex(RuntimeError, 'permission denied'):
                adb.list_remote_files('device', 'com.example.app')


class WorkerTests(unittest.TestCase):
    def test_quiet_stream_flushes_before_exit(self):
        lines, errors = [], []
        arrived = threading.Event()
        stopped = threading.Event()
        def receive(batch):
            lines.extend(batch)
            arrived.set()
        worker = LogThread([sys.executable, '-u', '-c',
                            "import time; print('single line', flush=True); time.sleep(10)"],
                           receive, errors.append, stopped.set)
        worker.start()
        try:
            self.assertTrue(arrived.wait(3), 'quiet stream never flushed')
            self.assertTrue(worker.is_alive())
        finally:
            worker.stop()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(lines, ['single line\n'])
        self.assertEqual(errors, [])
        self.assertTrue(stopped.is_set())

    def test_final_buffer_before_stopped(self):
        events = []
        worker = LogThread([sys.executable, '-c', "print('last')"],
                           lambda batch: events.append(('lines', batch)),
                           lambda error: events.append(('error', error)),
                           lambda: events.append(('stopped', None)))
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(events, [('lines', ['last\n']), ('stopped', None)])

    def test_manual_refresh_coalesces_and_preserves_failures(self):
        class Adb:
            calls = 0
            active = 0
            maximum = 0
            entered = threading.Event()
            release = threading.Event()
            def read_file(self, serial, remote_path, encoding):
                self.calls += 1
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                try:
                    if self.calls == 2:
                        self.entered.set()
                        self.release.wait(3)
                        raise RuntimeError('offline')
                    return f'snapshot {self.calls}'
                finally:
                    self.active -= 1
        adb = Adb()
        content, errors = [], []
        first = threading.Event()
        third = threading.Event()
        stopped = threading.Event()
        def receive(value):
            content.append(value)
            (first if len(content) == 1 else third).set()
        worker = FilePollingThread(adb, 'device', '/log', receive, errors.append, stopped.set)
        worker.start()
        try:
            self.assertTrue(first.wait(2))
            time.sleep(0.15)
            self.assertEqual(adb.calls, 1)
            worker.trigger_refresh()
            self.assertTrue(adb.entered.wait(2))
            for _ in range(20):
                worker.trigger_refresh()
            adb.release.set()
            self.assertTrue(third.wait(2))
        finally:
            adb.release.set()
            worker.stop()
            worker.join(3)
        self.assertEqual(adb.calls, 3)
        self.assertEqual(adb.maximum, 1)
        self.assertEqual(content, ['snapshot 1', 'snapshot 3'])
        self.assertIn('offline', errors[0])
        self.assertTrue(stopped.is_set())

    def test_stop_before_start(self):
        stopped = threading.Event()
        worker = LogThread([sys.executable, '-c', 'import time; time.sleep(10)'],
                           lambda _: None, lambda _: None, stopped.set)
        worker.stop()
        worker.start()
        worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertTrue(stopped.is_set())


class SessionTests(unittest.TestCase):
    def test_export_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'session.tmp.log'
            export = Path(directory) / 'export.txt'
            session = SessionLog(file)
            session.start()
            session.append('first\n')
            session.append('second\n')
            session.export(export)
            self.assertEqual(export.read_text(encoding='utf-8'), 'first\nsecond\n')
            session.snapshot('latest\n')
            session.export(export)
            self.assertEqual(export.read_text(encoding='utf-8'), 'latest\n')
            session.close()
            self.assertIsNone(session.file)
            session.discard()
            self.assertFalse(file.exists())


if __name__ == '__main__':
    unittest.main()
