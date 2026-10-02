from __future__ import annotations

import json

CLAUDE_TOOL_ARGUMENTS = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}

PI_TOOL_ARGUMENTS = {
    "bash": "command",
    "read": "path",
    "edit": "path",
    "write": "path",
    "grep": "pattern",
    "find": "pattern",
    "ls": "path",
}


def parse_event(line: bytes) -> dict | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def summarize_stream_json(event: dict) -> str | None:
    if event.get("type") != "assistant":
        return None
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    for block in reversed(content):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return _tool_action(block.get("name"), block.get("input"), CLAUDE_TOOL_ARGUMENTS)
    return None


def summarize_codex_json(event: dict) -> str | None:
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    kind = item.get("type") or item.get("item_type")
    expected = "item.started" if kind == "command_execution" else "item.completed"
    if event.get("type") != expected:
        return None
    return _codex_item_action(item)


def summarize_pi_json(event: dict) -> str | None:
    if event.get("type") != "tool_execution_start":
        return None
    return _tool_action(event.get("toolName"), event.get("args"), PI_TOOL_ARGUMENTS)


def _tool_action(name: object, arguments: object, table: dict[str, str]) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    key = table.get(name)
    value = arguments.get(key) if isinstance(arguments, dict) and key is not None else None
    if isinstance(value, str) and value.strip():
        return f"{name}: {_one_line(value)}"
    return name


def _codex_item_action(item: dict) -> str | None:
    kind = item.get("type") or item.get("item_type")
    if kind == "command_execution":
        return _command_action(item.get("command"))
    if kind == "file_change":
        changes = item.get("changes")
        paths = [entry.get("path") for entry in changes if isinstance(entry, dict)] if isinstance(changes, list) else []
        names = [path for path in paths if isinstance(path, str) and path]
        if names:
            return f"patch: {_one_line(' '.join(names))}"
        return "patch"
    return None


def _command_action(command: object) -> str | None:
    if isinstance(command, str) and command.strip():
        return f"exec: {_one_line(command)}"
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return f"exec: {_one_line(' '.join(command))}"
    return None


def _one_line(text: str) -> str:
    return " ".join(text.split())
