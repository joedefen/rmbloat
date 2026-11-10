"""The half of the release gate that belongs to *this* project.

`bless` itself is tested once, in the repository that emits it
([`../../blessd/tests/test_bless.py`](../../blessd/tests/test_bless.py)): the
branch plan, the refusals, the prompt, the index check and the emitter are the
same wherever the gate is vendored.  What is not the same is the identity it
derives from this tree -- read at start-up, never written into the tool -- and
the inspector it runs before an upload, [`inspect_dist.py`](inspect_dist.py),
which asserts this project's metadata.  Those two are pinned here.
"""

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


bless = load_module('bless_gate', ROOT / 'bless')
inspect_dist = load_module('inspect_dist', ROOT / 'tests' / 'inspect_dist.py')

PROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
VERSION = next(line.split('"')[1] for line in
               (ROOT / 'rmbloat' / '__init__.py').read_text(encoding='utf-8').splitlines()
               if line.startswith('__version__'))


# --- the identity the vendored gate derives from this tree -----------------

class GateIdentityTests(unittest.TestCase):
    """`bless` must read this tree's identity, never carry its own copy."""

    def test_this_tree_derives_its_own_identity(self):
        name = PROJECT['name']

        self.assertEqual(bless.PROJECT['name'], name)
        self.assertEqual(bless.PROJECT['module'], name.replace('-', '_'))
        self.assertEqual(bless.PROJECT['console'], 'rmbloat')
        self.assertIn(bless.PROJECT['console'], list(PROJECT['scripts']))
        self.assertEqual(bless.PROJECT['inspector'], 'tests/inspect_dist.py')
        # This tree has a development branch and the gate aligns it with the
        # trunk; the pair is declared in [tool.bless] on purpose.
        self.assertEqual(bless.PROJECT['branches'], ('main', 'dev'))
        self.assertEqual(bless.PYPI_URL,
                         f'https://pypi.org/pypi/{name}/json')
        self.assertEqual(bless.TESTPYPI_URL,
                         f'https://test.pypi.org/pypi/{name}/json')
        self.assertEqual(bless.read_version(), VERSION)

    def test_the_version_is_declared_in_exactly_one_place(self):
        """`dynamic = ["version"]` means pyproject must not repeat the number."""
        self.assertEqual(PROJECT.get('dynamic'), ['version'])
        self.assertNotIn('version', PROJECT)

    def test_the_smoke_run_needs_no_special_arguments(self):
        """`bless` imports the package and prints `__version__`; that is enough."""
        self.assertEqual(bless.PROJECT['smoke_args'], [])

    def test_the_console_script_reports_the_release_number(self):
        """What the gate's smoke stage runs: `rmbloat --version`, with no state."""
        script = Path(sys.executable).with_name('rmbloat')
        if not script.exists():
            self.skipTest(f'{script} is not installed; run `bt penv` first')
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, 'HOME': home,
                   'XDG_CONFIG_HOME': os.path.join(home, '.config'),
                   'XDG_CACHE_HOME': os.path.join(home, '.cache'),
                   'COLUMNS': '80'}
            result = subprocess.run([str(script), '--version'], capture_output=True,
                                    text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'rmbloat {VERSION}')


# --- the shared inspector ------------------------------------------------

def make_wheel(path, *, summary=None, dependencies=None, include_tests=False,
               modules=True):
    metadata = [
        'Metadata-Version: 2.4',
        'Name: rmbloat',
        f'Version: {VERSION}',
        f'Summary: {summary or PROJECT["description"]}',
        f'Requires-Python: {PROJECT["requires-python"]}',
        *[f'Requires-Dist: {dependency}' for dependency in
          (dependencies if dependencies is not None
           else PROJECT['dependencies'])],
        f'Keywords: {", ".join(PROJECT.get("keywords", []))}',
        'License-Expression: MIT',
        'License-File: LICENSE',
        '',
        'A description body.',
    ]
    names = ([f'rmbloat/{module.name}'
              for module in (ROOT / 'rmbloat').glob('*.py')] if modules else [])
    names.append(f'rmbloat-{VERSION}.dist-info/licenses/LICENSE')
    if include_tests:
        names.append('tests/test_smoke.py')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'rmbloat-{VERSION}.dist-info/METADATA',
                         '\n'.join(metadata))
        for name in names:
            archive.writestr(name, '')
    return Path(path)


def make_sdist(path, *, version=VERSION, include_tests=False):
    base = f'rmbloat-{version}'
    members = {
        f'{base}/pyproject.toml': '',
        f'{base}/README.md': '',
        f'{base}/LICENSE': '',
        f'{base}/rmbloat/__init__.py': f'__version__ = "{version}"\n',
        f'{base}/rmbloat/rmbloat.py': '',
    }
    if include_tests:
        members[f'{base}/tests/test_smoke.py'] = ''
    with tarfile.open(path, 'w:gz') as archive:
        for name, text in members.items():
            payload = text.encode('utf-8')
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return Path(path)


class InspectorTests(unittest.TestCase):
    """The inspector must accept this project's build and reject a different one."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_a_correct_wheel_and_sdist_pass(self):
        inspect_dist.check_all(self.wheel('ok.whl'), self.sdist('ok.tar.gz'))

    def test_the_inspector_rejects_a_summary_that_is_not_this_project(self):
        wheel = self.wheel('summary.whl',
                           summary='A GTK tray applet for power management')
        with self.assertRaises(inspect_dist.DistributionError) as caught:
            inspect_dist.check_wheel(wheel)
        self.assertIn('Summary', str(caught.exception))

    def test_the_inspector_rejects_an_undeclared_runtime_dependency(self):
        wheel = self.wheel('extra.whl', dependencies=[
            *PROJECT['dependencies'], 'pydbus>=0.6.0'])
        with self.assertRaises(inspect_dist.DistributionError) as caught:
            inspect_dist.check_wheel(wheel)
        self.assertIn('Requires-Dist', str(caught.exception))

    def test_the_inspector_keeps_wheel_contents_honest(self):
        with self.assertRaisesRegex(inspect_dist.DistributionError, 'tests/'):
            inspect_dist.check_wheel(self.wheel('tests.whl', include_tests=True))

        with self.assertRaisesRegex(inspect_dist.DistributionError, 'missing'):
            inspect_dist.check_wheel(self.wheel('bare.whl', modules=False))

    def test_the_sdist_must_carry_the_package_and_its_version(self):
        stale = '0.0.1' if VERSION != '0.0.1' else '0.0.2'
        with self.assertRaisesRegex(inspect_dist.DistributionError, stale):
            inspect_dist.check_sdist(self.sdist('old.tar.gz', version=stale))

        # The rehearsal overrides the version, and only that.
        inspect_dist.check_sdist(
            self.sdist('rehearsal.tar.gz', version='9.9.9.99.0'),
            version='9.9.9.99.0')

    def wheel(self, name, **kwargs):
        return make_wheel(self.tmp / name, **kwargs)

    def sdist(self, name, **kwargs):
        return make_sdist(self.tmp / name, **kwargs)


if __name__ == '__main__':
    unittest.main()
