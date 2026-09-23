#!/usr/bin/env python3
"""Clear the macOS UF_HIDDEN flag from the venv's editable `.pth` files.

**Why this exists.** On macOS, `uv sync` writes its editable-install `.pth` files via a
temporary dotfile and the rename leaves the BSD `UF_HIDDEN` flag set. CPython 3.12+ skips
hidden `.pth` files on purpose (`site.addpackage`), so the installed `aiden` console script
fails with:

    ModuleNotFoundError: No module named 'aiden'

even though the package installed correctly. The flag is a property of the file, not of the
name, so `chflags nohidden` is the fix and it survives until the next time uv rewrites them.

Run after `uv sync`, or just use `uv run python -m aiden ...` from the repo root, which does
not depend on `.pth` handling at all.

    uv run python scripts/dev_setup.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    site_packages = sorted(REPO.glob(".venv/lib/python*/site-packages"))
    if not site_packages:
        print("no venv found; run `uv sync` first")
        return 1

    pths = sorted(p for d in site_packages for p in d.glob("*.pth"))
    if not pths:
        print("no .pth files found; nothing to do")
        return 0

    hidden: list[Path] = []
    for path in pths:
        try:
            status = subprocess.run(
                ["ls", "-lO", str(path)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError:
            continue
        if " hidden" in status:
            hidden.append(path)

    if not hidden:
        print(f"{len(pths)} .pth file(s) checked; none hidden")
        return 0

    for path in hidden:
        subprocess.run(["chflags", "nohidden", str(path)], check=False)
        print(f"cleared hidden flag: {path.relative_to(REPO)}")

    print(f"\nfixed {len(hidden)} file(s). `aiden` should work now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
