from __future__ import annotations

import re
from collections.abc import Mapping

from .artifacts.mask import BlockCategory
from .masking import (
    MaskError,
    TableEntry,
    read_control_sequence,
    read_environment_name,
    skip_code_environment,
    skip_verb,
)

BYTE_TEXT_ENCODING = "latin-1"

COMMENTED_CODE_ENVIRONMENTS = frozenset({"comment"})

URL_COMMANDS = frozenset({"url"})

URL_ARGUMENT_RE = re.compile(r"\{([^{}\n]*)\}")

UNESCAPED_PERCENT_RE = re.compile(r"(?<!\\)%")

END_DOCUMENT_LINE_RE = re.compile(r"[ \t]*\\end\{document\}")


def strip_comment_text(source: bytes, table: Mapping[str, TableEntry]) -> bytes:
    text = source.decode(BYTE_TEXT_ENCODING)
    parts: list[str] = []
    cursor = 0
    position = 0
    length = len(text)
    while position < length:
        character = text[position]
        if character == "%":
            line_end = _line_end(text, position)
            parts.append(text[cursor : position + 1])
            cursor = position = line_end
            continue
        if character == "\n":
            position += 1
            if END_DOCUMENT_LINE_RE.match(text, position):
                line_end = _line_end(text, position)
                parts.append(text[cursor : line_end + 1])
                cursor = length
                break
            continue
        if character != "\\":
            position += 1
            continue
        name, after = read_control_sequence(text, position)
        if name == "verb":
            position = skip_verb(text, after)
        elif name == "begin":
            position = _skip_code_environment(text, after, table)
        elif name in URL_COMMANDS:
            match = URL_ARGUMENT_RE.match(text, after)
            if match is None:
                position = after
                continue
            parts.append(text[cursor : match.start(1)])
            parts.append(UNESCAPED_PERCENT_RE.sub(r"\\%", match.group(1)))
            cursor = position = match.end(1)
        else:
            position = after
    parts.append(text[cursor:])
    return "".join(parts).encode(BYTE_TEXT_ENCODING)


def _skip_code_environment(text: str, after_begin: int, table: Mapping[str, TableEntry]) -> int:
    environment, body_start = read_environment_name(text, after_begin)
    if environment is None:
        return after_begin
    entry = table.get(environment) or table.get(environment.rstrip("*"))
    if entry is None or entry.category is not BlockCategory.CODE:
        return body_start
    if environment.rstrip("*") in COMMENTED_CODE_ENVIRONMENTS:
        return body_start
    try:
        return skip_code_environment(text, body_start, environment)
    except MaskError:
        return body_start


def _line_end(text: str, position: int) -> int:
    index = text.find("\n", position)
    return len(text) if index < 0 else index
