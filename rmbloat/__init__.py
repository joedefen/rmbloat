"""rmbloat: the smart video converter for media server owners.

The release number lives here and nowhere else.  ``pyproject.toml`` declares
``dynamic = ["version"]``, so the build backend reads this line, the ``./bless``
release gate reads it without importing the package, and every built artifact
reports it.
"""

__version__ = "1.0.1"
