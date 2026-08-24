"""Small subprocess adapter used by the local transport."""

from __future__ import annotations

import subprocess
from typing import Any, Protocol


class ProcessRunner(Protocol):
    """Callable contract implemented by subprocess.run and test doubles."""

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        """Run one exact argument array."""


def run_process(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Execute without a shell."""
    return subprocess.run(argv, shell=False, **kwargs)

