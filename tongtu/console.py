from __future__ import annotations

from rich.console import Console

console = Console(markup=False, soft_wrap=True)
error_console = Console(stderr=True, markup=False, soft_wrap=True)
