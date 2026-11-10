"""Regression tests for the ``./linter`` wrapper.

The wrapper preferred ``python -m pylint`` but silently fell back to whatever
``pylint`` was on ``PATH`` when the chosen interpreter could not import pylint.
That ran pylint under an interpreter without rmbloat's dependencies, so a clean
checkout reported bogus ``E0401`` import-errors (``console_window``,
``send2trash``) and exited 30.  The wrapper also ran pylint's full default check
set, which reports the style messages ruff already covers.

It now prefers the checkout's ``.venv``, refuses to run without an importable
pylint (with the install command), and passes ``--errors-only``.
"""

# pylint: disable=missing-function-docstring

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINTER = REPO_ROOT / 'linter'
PYPROJECT = REPO_ROOT / 'pyproject.toml'

# EX_CONFIG from sysexits(3): the tool is missing, the code is not at fault.
MISSING_TOOL_STATUS = 78


class LinterWrapperTests(unittest.TestCase):

    def setUp(self):
        if not LINTER.exists():
            self.skipTest('the repository has no linter script')
        self.bash = shutil.which('bash')
        if self.bash is None:
            self.skipTest('bash is required to run ./linter')

    def run_linter(self, *args, extra_env=None):
        env = dict(os.environ)
        env.update(extra_env or {})
        return subprocess.run(
            [self.bash, str(LINTER), *args],
            capture_output=True, text=True, env=env,
            cwd=str(REPO_ROOT), timeout=180, check=False)

    def test_an_interpreter_without_pylint_is_a_configuration_error(self):
        interpreter = '/bin/false'
        if not os.path.exists(interpreter):
            self.skipTest('/bin/false is unavailable')

        result = self.run_linter(extra_env={'PYTHON': interpreter})

        self.assertEqual(result.returncode, MISSING_TOOL_STATUS, result.stderr)
        self.assertIn(f'pylint is not importable by {interpreter}', result.stderr)
        self.assertIn('.[dev]', result.stderr,
                      'the message must say how to install pylint')

    def test_pylint_runs_with_errors_only_from_the_chosen_interpreter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stub = Path(tmpdir) / 'pylint.py'
            stub.write_text(
                'import sys\n'
                "print('STUB-PYLINT ' + ' '.join(sys.argv[1:]))\n",
                encoding='utf-8')
            result = self.run_linter(
                'rmbloat',
                extra_env={
                    'PYTHON': sys.executable,
                    'PYTHONPATH': os.pathsep.join(
                        [tmpdir, os.environ.get('PYTHONPATH', '')]),
                })

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--errors-only', result.stdout,
                      'style messages belong to ruff, not this gate')
        self.assertIn('rmbloat', result.stdout)

    def test_the_wrapper_has_no_silent_interpreter_fallback(self):
        script = LINTER.read_text(encoding='utf-8')
        self.assertNotIn('PYLINT_CMD=(pylint)', script,
                         'a PATH pylint runs with the wrong dependencies')
        self.assertIn('--errors-only', script)

    def test_pylint_is_a_declared_dev_dependency(self):
        import tomllib  # pylint: disable=import-outside-toplevel

        project = tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))['project']
        dev = project.get('optional-dependencies', {}).get('dev', [])

        names = {dependency.split('>')[0].split('=')[0].split('[')[0].strip().lower()
                 for dependency in dev}
        self.assertIn('pylint', names, f'dev extra is {dev}')


if __name__ == '__main__':
    unittest.main()
