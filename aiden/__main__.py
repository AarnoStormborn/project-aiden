"""Allow ``python -m aiden`` as a path-independent entry point.

The packaged ``aiden`` console script relies on an editable ``.pth`` file. On macOS that
file can carry the BSD ``UF_HIDDEN`` flag, which CPython 3.12+ deliberately skips, making
the console script fail with ``ModuleNotFoundError: No module named 'aiden'`` even though
the package installs correctly. ``python -m aiden`` does not depend on ``.pth`` handling.

    chflags nohidden .venv/lib/python*/site-packages/*.pth   # one-line fix if needed
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
