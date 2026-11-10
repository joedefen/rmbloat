"""Regression tests for the ``StructuredLogger.put()`` duplicate-``data`` bug.

``put()`` is the backward-compatible equivalent of ``RotatingLogger.put()``
and accepts structured ``data``.  It used to forward ``data=kwargs.get('data')``
while also splatting ``**kwargs`` into ``_create_log_entry()``, which raised
``TypeError: got multiple values for keyword argument 'data'`` whenever a
caller supplied ``data=``.
"""

import json
import tempfile
import unittest
from pathlib import Path

from rmbloat.StructuredLogger import StructuredLogger


class PutDataArgumentTest(unittest.TestCase):
    """``put()`` must accept ``data=`` exactly once."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.logger = StructuredLogger(
            'rmbloat_test', log_dir=Path(self._tmpdir.name))

    def _last_entry(self):
        lines = [
            line for line in self.logger.log_file.read_text(encoding='utf-8').splitlines()
            if line.strip()
        ]
        return json.loads(lines[-1])

    def test_put_accepts_data_keyword(self):
        # Must not raise TypeError any more.
        self.logger.put('CUSTOM_TAG', 'message', data={'key': 'value'})

        entry = self._last_entry()
        self.assertEqual(entry['level'], 'CUSTOM_TAG')
        self.assertEqual(entry['message'], 'message')
        self.assertEqual(entry['data'], {'key': 'value'})

    def test_put_without_data(self):
        self.logger.put('CUSTOM_TAG', 'plain message')

        entry = self._last_entry()
        self.assertEqual(entry['level'], 'CUSTOM_TAG')
        self.assertEqual(entry['message'], 'plain message')
        self.assertEqual(entry['data'], {})

    def test_put_joins_list_messages(self):
        self.logger.put('CUSTOM_TAG', ['first', 'second'])

        entry = self._last_entry()
        self.assertEqual(entry['message'], 'first\nsecond')


if __name__ == '__main__':
    unittest.main()
