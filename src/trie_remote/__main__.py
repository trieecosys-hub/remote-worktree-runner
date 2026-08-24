"""Module entrypoint for local and server runner modes."""

from __future__ import annotations

import sys

from trie_remote import local_cli, server_cli


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the requested runner mode."""
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0] not in {"local", "server"}:
        print(
            "usage: python -m trie_remote {local|server} ...",
            file=sys.stderr,
        )
        return 2

    mode = values.pop(0)
    if mode == "local":
        return local_cli.main(values)
    return server_cli.main(values)


if __name__ == "__main__":
    raise SystemExit(main())

