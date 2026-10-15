"""Fail validation when the installed native kernel is missing or incompatible."""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from endless_task import _native
    except ImportError as exc:
        print(f"Native kernel check failed: {exc}", file=sys.stderr)
        return 1

    if not _native.is_available():
        print(
            f"Native kernel check failed: {_native.unavailable_reason()}",
            file=sys.stderr,
        )
        return 1

    error = _native.check_abi()
    if error is not None:
        print(f"Native kernel check failed: {error}", file=sys.stderr)
        return 1

    available = {
        name for name, enabled, _detail in _native.capabilities() if enabled
    }
    missing = {"native.binding", "core.manifest"} - available
    if missing:
        print(
            "Native kernel check failed: missing capabilities "
            + ", ".join(sorted(missing)),
            file=sys.stderr,
        )
        return 1

    print(f"Native kernel available; ABI {_native.abi_version()} verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
