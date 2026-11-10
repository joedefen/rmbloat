"""Regression tests for audit finding H10.

``main()`` used to catch every ``Exception``, print a traceback, and return
normally, so the process always exited with status 0.  Shell scripts, cron,
systemd units, and CI could not tell a successful run from a failed
initialization.

``main()`` now returns an exit status (0 on success, 1 on failure) and the
``__main__`` entry point forwards it through ``sys.exit()``.
"""

import contextlib
import importlib
import io
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock


class _Dummy:
    """Minimal stand-in for console_window classes."""

    def __init__(self, *args, **kwargs):
        pass


@contextlib.contextmanager
def stubbed_runtime_dependencies():
    """Install fake third-party modules for the duration of a test."""
    previous = {}

    def install(name, **attributes):
        previous[name] = sys.modules.get(name)
        module = types.ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    appearance = install('console_window.appearance',
                         resolve_appearance=lambda **kwargs: ('default', True),
                         set_colors_enabled=lambda enabled: None,
                         colors_enabled=lambda: True,
                         semantic_attr=lambda key: 0)

    console_window = install('console_window', appearance=appearance)
    for name in ('ConsoleWindow', 'OptionSpinner', 'Screen', 'ScreenStack',
                 'ConsoleWindowOpts', 'IncrementalSearchBar', 'Context'):
        setattr(console_window, name, type(name, (_Dummy,), {}))

    theme_cls = type('Theme', (_Dummy,), {})
    theme_cls.list_all = classmethod(lambda cls: ('default',))
    theme_cls.set_active = classmethod(lambda cls, name: None)
    theme_cls.resolve = classmethod(lambda cls, name: name or 'default')
    theme_cls.create_picker_screen = classmethod(
        lambda cls, base, **kwargs: type('PickerScreen', (base,), {}))
    for attr in ('OLD_SUCCESS', 'HOTSWAP', 'ERROR', 'INFO', 'PROGRESS',
                 'WARNING'):
        setattr(theme_cls, attr, attr)
    console_window.Theme = theme_cls
    console_window.MONO_THEME = 'mono'

    # rmbloat imports `console_window.cli as display_cli` and calls both
    # helpers before the window exists.  The exit-status tests do not care
    # about display handling, so the stubs are no-ops.
    cli = install('console_window.cli',
                  add_display_arguments=lambda *args, **kwargs: None,
                  apply_display_arguments=lambda *args, **kwargs: None)
    console_window.cli = cli

    ruamel = install('ruamel')
    ruamel_yaml = install('ruamel.yaml', YAML=_Dummy)
    ruamel.yaml = ruamel_yaml

    install('send2trash', send2trash=lambda *args, **kwargs: None)

    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def purge_rmbloat_modules():
    """Drop cached rmbloat modules so module-level paths use the temp HOME."""
    for name in list(sys.modules):
        if name == 'rmbloat' or name.startswith('rmbloat.'):
            sys.modules.pop(name, None)


class MainExitStatusTests(unittest.TestCase):
    """`main()` must report failures to the shell as a non-zero status."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        home_patch = mock.patch.dict(
            os.environ, {'HOME': self.temp_dir.name}, clear=False)
        home_patch.start()
        self.addCleanup(home_patch.stop)

        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')

        # Isolate class-level singleton state between tests.
        previous_singleton = self.rmbloat.Converter.singleton
        self.rmbloat.Converter.singleton = None
        self.addCleanup(
            setattr, self.rmbloat.Converter, 'singleton', previous_singleton)

    def _fake_ini(self):
        vals = SimpleNamespace(
            allowed_codecs='x265',
            bloat_thresh=1600,
            files=[],
            full_speed=False,
            keep_backup=False,
            max_height=65536,
            merge_subtitles=False,
            min_shrink_pct=10,
            prefer_strategy='auto',
            quality=28,
            thread_cnt=4,
            use_trash=False,
        )
        return SimpleNamespace(
            vals=vals,
            config_file_path=os.path.join(self.temp_dir.name, 'config.ini'),
            write=mock.Mock(),
        )

    def test_successful_run_returns_zero(self):
        converter = mock.Mock()
        with mock.patch.object(
                self.rmbloat, 'IniManager', return_value=self._fake_ini()), \
             mock.patch.object(
                self.rmbloat, 'Converter', return_value=converter):
            status = self.rmbloat.main([])

        self.assertEqual(status, 0)
        converter.main_loop.assert_called_once_with()

    def test_unexpected_exception_returns_one_and_reports_traceback(self):
        stderr = io.StringIO()
        with mock.patch.object(
                self.rmbloat, 'IniManager',
                side_effect=RuntimeError('initialization exploded')):
            with contextlib.redirect_stderr(stderr), \
                    contextlib.redirect_stdout(io.StringIO()):
                status = self.rmbloat.main([])

        self.assertEqual(status, 1)
        printed = stderr.getvalue()
        self.assertIn('initialization exploded', printed)
        self.assertIn('Traceback', printed)

    def test_exception_path_stops_curses_window(self):
        win = SimpleNamespace(stop_curses=mock.Mock())
        self.rmbloat.Converter.singleton = SimpleNamespace(win=win)

        with mock.patch.object(
                self.rmbloat, 'IniManager',
                side_effect=RuntimeError('boom')):
            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                status = self.rmbloat.main([])

        self.assertEqual(status, 1)
        win.stop_curses.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
