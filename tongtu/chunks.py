from __future__ import annotations


def restore_padding(source: str, body: str) -> str:
    leading = source[: len(source) - len(source.lstrip())]
    trailing = source[len(source.rstrip()) :]
    return leading + body.strip() + trailing
