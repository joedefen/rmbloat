# rmbloat: working on the source tree

This is the developer companion to [`README.md`](../README.md), which documents
the tool for its users. Here we document the two things a maintainer needs and a
user does not: the **test gate** and the **release gate** (`./bless`).

This file lives in `tests/`, so its links point one directory up. The commands
assume the repository root, which is where the gate has to be run.

## Layout and single sources of truth

| Path | Owns |
| --- | --- |
| [`rmbloat/__init__.py`](../rmbloat/__init__.py) | `__version__`, and nothing else. The build backend reads it, `bless` reads the line without importing the package, and every built artifact reports it. |
| [`pyproject.toml`](../pyproject.toml) | Name, description, `requires-python`, runtime dependencies, the PEP 639 `license` and `license-files`, the `dev` extra, the `[tool.bless]` branch model, and the pytest, coverage and ruff configuration. |
| [`bless`](../bless) | The release gate, emitted verbatim from [`../../blessd/bless`](../../blessd/bless) by that repo's `build.py`. It holds no copy of this project's identity, and it is never edited here. |
| [`blessd/docs/release-strategy.md`](../../blessd/docs/release-strategy.md) | The release strategy every project shares: the branch model, the stages, the rehearsal, the credentials. This file documents only what is particular to `rmbloat`. |
| [`tests/inspect_dist.py`](inspect_dist.py) | The distribution inspector. `bless` refuses to upload a wheel and sdist it has not inspected with this file. |
| [`runner`](../runner) | Runs the tree from source (`rmbloat.rmbloat`) for debugging, under `.venv/bin/python`. It installs nothing and never writes to `~/.local/bin`, so it cannot displace a released `rmbloat`. |
| [`linter`](../linter) | An optional deeper check: pylint `--errors-only` under the project interpreter, for errors Ruff does not model. It is not part of the gate. |
| [`.gitignore`](../.gitignore) | Keeps gate artifacts (`build/`, `dist/`, `.coverage*`, caches, `*.whl`, `*.tar.gz`) out of `git status`, because the gate refuses a dirty tree. |

The version lives in exactly one place. `pyproject.toml` declares
`dynamic = ["version"]`, so flit reads `rmbloat/__init__.py`; a `X.Y.Z.99.N`
rehearsal rewrites that line only inside a throwaway copy.

Almost nothing needs a `[tool.bless]` table: `NAME = rmbloat`, `MODULE =
rmbloat`, the console script and the inspector are all derived from what is
already there. The one setting written out is `branches`, which names the
branches `publish` releases from -- see [the branch model](#the-branch-model-is-a-setting-not-a-convention)
below.

## Where the package lives

This tree follows the per-project environment model: the package lives in the
checkout, not in a shared site directory. Nothing here installs with
`pip --user`, so no project can shadow another's dependency.

| Thing | Where | Made by |
| --- | --- | --- |
| Editable package + this project's `dev` extra | `.venv/` in this tree | `bt penv` |
| The runnable console scripts | `~/.local/bin/rmbloat`, `~/.local/bin/rmbloatd` | `bt edit` (symlinks into `.venv/bin`) or `bt test` / `bt prod` / `pipx install` (pipx's own venv) |
| A released artifact, isolated per app | `~/.local/share/pipx/venvs/rmbloat` | `bt test` / `bt prod` |

Two invariants:

1. `.venv` is the **editable/debug** environment: the interpreter VS Code
   selects and the one `./bless`, pytest and a debugger run under, so
   breakpoints land in the tree. It holds an editable install
   (`-e '.[dev]'`), never a built copy.
2. Exactly **one owner** of `~/.local/bin/rmbloat` at a time -- `bt edit` links
   it into `.venv/bin`, `bt test`/`bt prod` let pipx own it -- and no command
   claims it with `pip --user`.

Run the source without owning the entry point:

    ./runner                      # re-execs into .venv/bin/python
    .venv/bin/python ./runner     # the same, spelled out

`runner` installs nothing and never writes to `~/.local/bin`, so a source run
and a released install cannot collide. When the command on `PATH` should follow
the tree instead, hand the entry point to it:

    bt edit        # ~/.local/bin/rmbloat -> .venv/bin/rmbloat

`bt edit` and `bt test`/`bt prod` are a switch, not two installs: the last one
run owns the link. Do not `pipx install --editable` for this project; that would
create a second editable install `bt edit` does not manage. `bt status` prints
the owner, pipx's own record, the editable `.venv` and the VS Code interpreter,
plus any leftover copy from the old model that `bt clean` removes.

VS Code selects `.venv` (that is the `Editable .venv` path `bt status` prints);
to debug a released artifact instead, select
`~/.local/share/pipx/venvs/rmbloat/bin/python`. This project ships no systemd
unit. If one is added for `rmbloatd`, name the stable path so a restart
re-resolves the link, and switching between edit and release mode changes what
the service runs without a reinstall:

    ExecStart=%h/.local/bin/rmbloatd

## The test gate

Build what the gate needs with `bt`, which puts the editable package plus the
`dev` extra into this tree's `.venv` (and the gate's tool floors with it):

    bt penv        # from the project root; `bt penv rmbloat` from elsewhere

`bless` runs every stage with `sys.executable`, so it has to be *that*
interpreter -- the one holding `console-window` and the dev extra. Activate the
virtualenv first, or start the gate by hand:

    .venv/bin/python ./bless

Then the commands `bless` runs, in the same order, on the same interpreter:

    python -m ruff check .
    python -m compileall -q rmbloat tests
    python -m pytest -q --cov=rmbloat --cov-report=term-missing

The coverage floor (60%) is enforced through
`[tool.coverage.report] fail_under`. It was set just below the measured number
when the gate was adopted; raise it when the number rises, never lower it.

The Ruff selection is committed rather than inherited
(`[tool.ruff.lint] select = ["E4", "E7", "E9", "F", "I", "B"]`,
`target-version = "py311"`, `line-length = 120`) so that no user or global
configuration -- and no change in Ruff's default rule set -- can change what the
gate enforces. Formatting and annotation-modernisation rules are deliberately
excluded: this tree predates PEP 604/585 annotations, and that rewrite is
unrelated to the gate.

`./linter` remains available for the errors Ruff does not model (a duplicate
method, an undefined name); it is not part of the gate because it needs pylint,
which is in the `dev` extra:

    ./linter                 # pylint --errors-only over ./rmbloat

## The release gate: `./bless`

`./bless` takes no arguments and has no flag that skips a stage. It always runs
everything, then prints what it has in hand and asks one question:

    ====> Enter test or publish or quit:

Only `test` and `publish` are accepted, spelled in full; anything else is asked
again. Enter, `quit` or Ctrl-C stops with nothing tagged, uploaded or pushed.

**Stages, in order**

1. **preflight** -- clean tree, the `vX.Y.Z` tag absent, `twine>=7` and
   `packaging>=26` present, `python -m build` available.
2. **index check** -- refuses if `__version__` is already on PyPI, printing
   every published version, before anything is built or uploaded.
3. **gates** -- the three commands above.
4. **build** -- into `build/bless-<version>/`, wiped first, so no older
   artifact can be sent by mistake.
5. **inspect** -- `tests/inspect_dist.py` against the wheel and the sdist.
6. **check** -- `twine check --strict` on both files.
7. **smoke** -- installs the built wheel into a throwaway venv and runs the
   installed console script as `rmbloat --version`, which must print
   `rmbloat <version>`. `main()` answers that flag before reading or writing
   any configuration, so the check works on a machine with no saved state and
   does not create a config file as a side effect.
8. **branch plan** -- refuses a detached HEAD, a branch that is not one of the
   release branches, a release branch missing locally or on origin, two
   branches that have diverged, or a push that would not fast-forward.

**`test`** is the rehearsal and never spends the working version. It copies the
tree to `build/bless-test-<version>/src`, rewrites `__version__` to
`X.Y.Z.99.N` (99 keeps it clear of real releases; `N` is one past the highest
suffix TestPyPI already holds for that base), builds, inspects and smoke-tests
the copy, and uploads only to `test.pypi.org`. No tag, no branch, no push. It
prints the install line, which needs `python -m pip cache purge` first and
`--extra-index-url https://pypi.org/simple/`, because TestPyPI carries no
dependencies.

**`publish`** fast-forwards the release branches to one commit, tags `vX.Y.Z`,
uploads the two inspected files to PyPI, verifies the index, and pushes those
branches and the tag.

**Credentials** are resolved per index -- `TWINE_PASSWORD` for PyPI,
`TWINE_TESTPYPI_PASSWORD` for TestPyPI, each overruling the `~/.pypirc` section
whose `repository` matches that index's upload URL -- so a PyPI token is never
spent on a rehearsal. A missing credential is a refusal before `twine` starts.

**Verification** reads the index back after an upload. The read is cache-busted
and patient (up to a quarter of an hour), because both indexes list new releases
through a CDN that lags; a release that is not listed yet is reported as lag
with the project page to check, never as a failed upload, since an accepted
upload cannot be withdrawn.

There is no CI publish job, on purpose: the upload is irreversible and is run by
the owner from the development tree, so the safety lives in one inspectable
command. CI stays a gate.

## The branch model is a setting, not a convention

`bless` publishes only from a branch it was told about, and pushes every branch
it was told about. Which ones those are is `[tool.bless] branches` in
[`pyproject.toml`](../pyproject.toml), and there are two shapes:

* `branches = ["main"]` -- one release branch. Nothing is aligned, and `publish`
  pushes `main` and the tag.
* `branches = ["main", "dev"]` -- the default when the setting is absent. While
  you are on `main`, `dev` does not have to exist yet: the trunk alone satisfies
  the model, so `publish` releases from `main` and pushes only what exists. Once
  `dev` has been created it also has to exist as `origin/dev`, and whichever of
  the two lags is fast-forwarded onto the other before the tag, so a release
  always leaves them at one commit.

This project declares the pair, because `dev` exists and is where the work
lands. Either way the gate refuses a branch it was not told about, a branch it
has to push that is missing on origin, two branches that have diverged, and a
push that would not fast-forward. What a single branch cannot do is notice that
a development branch was left behind: merging `dev` into `main` stays your job,
and the pair exists to make it the gate's job instead.

## What the inspector asserts

[`tests/inspect_dist.py`](inspect_dist.py) is the one implementation, used by
`bless` and, once CI is wired up, by CI; expectations are read from
`pyproject.toml` and the package, never from the artifact under test.

* **wheel** -- `Name`, `Version`, `Summary`, `Requires-Python`,
  `License-Expression` and the *runtime* `Requires-Dist` names match
  `pyproject.toml`; every declared `license-files` entry is listed in
  `License-File`; every `rmbloat/*.py` is present; a `LICENSE` is carried; no
  `tests/`, no `.venv`, no `__pycache__`, no `.pyc`.
* **sdist** -- carries `pyproject.toml`, `README.md`, `LICENSE`,
  `rmbloat/__init__.py`, `rmbloat/rmbloat.py`; the `__version__` inside the
  sdist equals the artifact version; no venv or bytecode. The sdist is
  deliberately lean: flit's default payload is enough and `tests/` need not
  ride along.

Run it by hand on any built pair:

    python tests/inspect_dist.py build/bless-1.0.0/rmbloat-1.0.0-py3-none-any.whl \
                                build/bless-1.0.0/rmbloat-1.0.0.tar.gz

## Releasing, in three steps

1. Bump `__version__` in [`rmbloat/__init__.py`](../rmbloat/__init__.py) and
   commit.
2. Run `./bless`. The gate runs in full.
3. Answer `test` to rehearse on TestPyPI, then run `./bless` again and answer
   `publish` -- or go straight to `publish`.

`[tool.bless] branches` names what `publish` pushes. The trunk in that list must
exist and track origin; a development branch joins the release once it exists,
and from then on it has to exist on origin too. `publish` leaves every branch it
pushes at the tagged commit -- here `main` and `dev`. Everything else about the
release is one of the stages above, and the shared half lives in
[`blessd/docs/release-strategy.md`](../../blessd/docs/release-strategy.md).

## Notes on this adoption

* This section is one run of
  [`../../blessd/docs/adopting.md`](../../blessd/docs/adopting.md), which is the
  procedure written for whoever converts the next project. If the two ever
  disagree, that document is the procedure and this is the record of one run.
* [`bless`](../bless) is emitted, byte for byte, from
  [`../../blessd/bless`](../../blessd/bless) -- SHA-256
  `5de3ad1a4c0a3dc9e3ff52d864bbcd4791e2e2ba545fe08ed36eadef870577fe` when this
  was written. It is not edited here: a change is made in `blessd` and re-emitted
  with `python3 ../../blessd/build.py --into .` (or `--force` to replace a
  differing copy), which replaces the file and prints the new hash.
* `rmbloat` needed five things the gate reads and could not invent: one version
  location (`__version__` in `rmbloat/__init__.py` plus
  `dynamic = ["version"]`), an inspector, the `dev` extra with `build`,
  `twine>=7`, `packaging>=26`, `pytest` and `pytest-cov`, a console script that
  answers `--version` with the release number (the entry point existed, the
  flag did not) and a branch model -- `branches = ["main", "dev"]`. The
  metadata also moved to the PEP 639 `license` expression and `license-files`,
  which is why the build backend floor is `flit_core>=3.12`.
* The Ruff rule set was added with the gate, so the whole tree was brought to a
  clean `ruff check .` once: six violations were fixed by hand (four `E701`,
  one `B904`, one `B011`) and the import blocks were sorted by
  `ruff check --fix`. [`tests/test_bless.py`](test_bless.py) pins the identity
  this tree derives and the inspector it uploads behind; what the gate itself
  does -- branch plan, refusals, prompt, index check, emitter -- is tested once,
  in [`../../blessd/tests/test_bless.py`](../../blessd/tests/test_bless.py).
* CI is not wired up yet. When it is, it must run the same three gate commands
  and the same inspector, never an equivalent.
