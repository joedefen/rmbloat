"""Shared hermetic stubs for tests that import :mod:`rmbloat.rmbloat`.

The curses UI dependencies (``console_window``, ``ruamel.yaml`` and
``send2trash``) are not installed in a bare checkout.  These stubs let the
regression tests import the real module and exercise non-UI helpers without
starting curses or touching user configuration.
"""

import contextlib
import sys
import types


class Dummy:
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

    appearance = install(
        'console_window.appearance',
        resolve_appearance=lambda **kwargs: ('default', True),
        set_colors_enabled=lambda enabled: None,
        colors_enabled=lambda: True,
        semantic_attr=lambda key: 0,
    )

    console_window = install('console_window', appearance=appearance)
    for name in ('ConsoleWindow', 'OptionSpinner', 'Screen', 'ScreenStack',
                 'ConsoleWindowOpts', 'IncrementalSearchBar', 'Context'):
        setattr(console_window, name, type(name, (Dummy,), {}))

    theme_cls = type('Theme', (Dummy,), {})
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

    cli = install('console_window.cli',
                  add_display_arguments=lambda *args, **kwargs: None,
                  apply_display_arguments=lambda *args, **kwargs: None)
    console_window.cli = cli

    ruamel = install('ruamel')
    ruamel_yaml = install('ruamel.yaml', YAML=Dummy)
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
