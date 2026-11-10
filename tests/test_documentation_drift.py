"""Regression tests: documentation must describe what the code actually does.

Two drifts must not come back:

* ``IniManager.default_config_dir()`` called itself the single source of truth
  for the probe cache and its lock file, although both live in
  ``$XDG_CACHE_HOME/rmbloat``; only ``config.ini`` and ``state.json`` are in
  ``~/.config/rmbloat``.
* The ``--help`` snapshot in ``README.md`` still advertised
  ``SAMPLE.{input-file}`` (and other stale option text), while the option help
  and the code produce ``SAMPLE.<crf>.<standard_name>``.

The help test compares the README against each declared option's help string
rather than against formatted ``--help`` output, so it is immune to argparse's
version-specific column layout.
"""

# pylint: disable=protected-access,missing-function-docstring

import argparse
import contextlib
import importlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rmbloat.IniManager import IniManager

from tests.runtime_stubs import purge_rmbloat_modules, stubbed_runtime_dependencies

README = Path(__file__).resolve().parent.parent / 'README.md'


class ConfigDirectoryDocstringTests(unittest.TestCase):
    """The config directory is not the cache directory."""

    def test_docstring_scopes_the_helper_to_config_and_state(self):
        doc = IniManager.default_config_dir.__doc__ or ''

        self.assertIn('config.ini', doc)
        self.assertIn('state.json', doc)
        self.assertIn('default_cache_dir', doc,
                      'the docstring must point at the cache helper')
        self.assertNotIn('single source of truth', doc)
        self.assertNotIn('probe-cache lock file', doc)


class ReadmeHelpSnapshotTests(unittest.TestCase):
    """Every option's help text must appear in the README snapshot."""

    def setUp(self):
        dependencies = stubbed_runtime_dependencies()
        dependencies.__enter__()
        self.addCleanup(dependencies.__exit__, None, None, None)

        purge_rmbloat_modules()
        self.addCleanup(purge_rmbloat_modules)

        self.rmbloat = importlib.import_module('rmbloat.rmbloat')
        previous_singleton = self.rmbloat.Converter.singleton
        self.rmbloat.Converter.singleton = None
        self.addCleanup(
            setattr, self.rmbloat.Converter, 'singleton', previous_singleton)

        self._config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._config_dir.cleanup)

    def fake_ini(self):
        vals = SimpleNamespace(
            allowed_codecs='x265', bloat_thresh=1600, files=[],
            full_speed=False, keep_backup=False, max_height=65536,
            merge_subtitles=False, min_shrink_pct=10, prefer_strategy='auto',
            quality=28, thread_cnt=4, use_trash=False)
        return SimpleNamespace(
            vals=vals,
            config_file_path=os.path.join(self._config_dir.name, 'config.ini'),
            write=mock.Mock())

    def capture_parser(self):
        """Run ``main()`` far enough to build the real ArgumentParser."""
        captured = {}

        def spy(parser, *args, **kwargs):
            captured['parser'] = parser
            raise SystemExit(0)

        with mock.patch.object(argparse.ArgumentParser, 'parse_args', spy), \
                mock.patch.object(self.rmbloat, 'IniManager',
                                  return_value=self.fake_ini()), \
                mock.patch.object(self.rmbloat, 'Converter'):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.rmbloat.main([])
        return captured['parser']

    def test_readme_documents_every_option_help(self):
        parser = self.capture_parser()
        # Whitespace is removed entirely rather than collapsed, because argparse
        # wraps at a hyphen ("auto-\nselect") and that is not a documentation
        # difference.
        compact_readme = ''.join(README.read_text(encoding='utf-8').split())

        checked = 0
        for action in parser._actions:
            if action.dest == 'help' or not action.help:
                continue
            expected = ''.join(action.help.split())
            self.assertIn(expected, compact_readme,
                          f'the README --help snapshot is stale for {action.dest!r}')
            checked += 1

        self.assertGreater(checked, 20, 'the parser should declare many options')

    def test_readme_names_the_sample_files_the_code_produces(self):
        readme = README.read_text(encoding='utf-8')

        self.assertIn('SAMPLE.<crf>.<output>', readme)
        self.assertNotIn('SAMPLE.{input-file}', readme)


if __name__ == '__main__':
    unittest.main()
