from __future__ import annotations

import json

from tongtu.model.events import (
    CLAUDE_TOOL_ARGUMENTS,
    PI_TOOL_ARGUMENTS,
    parse_event,
    summarize_codex_json,
    summarize_pi_json,
    summarize_stream_json,
)


def stream_line(*blocks: dict) -> bytes:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}}).encode("utf-8")


def tool_use(name: str, arguments: dict) -> dict:
    return {"type": "tool_use", "id": "toolu_01", "name": name, "input": arguments}


def test_argument_tables_share_one_shape() -> None:
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in CLAUDE_TOOL_ARGUMENTS.items())
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in PI_TOOL_ARGUMENTS.items())
    assert set(PI_TOOL_ARGUMENTS) == {"read", "bash", "edit", "write", "grep", "find", "ls"}


def test_stream_json_bash_shows_the_command() -> None:
    event = json.loads(stream_line(tool_use("Bash", {"command": "latexmk -xelatex flat.tex"})))
    assert summarize_stream_json(event) == "Bash: latexmk -xelatex flat.tex"


def test_stream_json_edit_shows_the_file_path() -> None:
    event = json.loads(stream_line(tool_use("Edit", {"file_path": "flat.tex", "old_string": "a", "new_string": "b"})))
    assert summarize_stream_json(event) == "Edit: flat.tex"


def test_stream_json_grep_shows_the_pattern() -> None:
    event = json.loads(stream_line(tool_use("Grep", {"pattern": "usepackage"})))
    assert summarize_stream_json(event) == "Grep: usepackage"


def test_stream_json_unknown_tool_shows_the_name_only() -> None:
    event = json.loads(stream_line(tool_use("TodoWrite", {"todos": []})))
    assert summarize_stream_json(event) == "TodoWrite"


def test_stream_json_missing_argument_shows_the_name_only() -> None:
    event = json.loads(stream_line(tool_use("Bash", {})))
    assert summarize_stream_json(event) == "Bash"


def test_stream_json_newlines_collapse_to_one_line() -> None:
    event = json.loads(stream_line(tool_use("Bash", {"command": "latexmk \\\n  -xelatex \\\n  flat.tex"})))
    assert summarize_stream_json(event) == "Bash: latexmk \\ -xelatex \\ flat.tex"


def test_stream_json_last_tool_use_wins() -> None:
    event = json.loads(stream_line(tool_use("Read", {"file_path": "flat.log"}), tool_use("Bash", {"command": "ls"})))
    assert summarize_stream_json(event) == "Bash: ls"


def test_stream_json_text_only_turn_is_not_an_action() -> None:
    event = json.loads(stream_line({"type": "text", "text": "看一下编译日志"}))
    assert summarize_stream_json(event) is None


def test_stream_json_other_event_types_are_not_actions() -> None:
    for event in ({"type": "system", "subtype": "init"}, {"type": "user"}, {"type": "result", "is_error": False}):
        assert summarize_stream_json(event) is None


def test_codex_json_old_protocol_lines_are_not_actions() -> None:
    event = {"id": "0", "msg": {"type": "exec_command_begin", "command": ["bash", "-lc", "latexmk flat.tex"]}}
    assert summarize_codex_json(event) is None


def test_codex_json_item_command_execution_shows_the_command() -> None:
    event = {"type": "item.started", "item": {"type": "command_execution", "command": "latexmk flat.tex"}}
    assert summarize_codex_json(event) == "exec: latexmk flat.tex"


def test_codex_json_item_file_change_shows_the_paths() -> None:
    event = {
        "type": "item.completed",
        "item": {"type": "file_change", "changes": [{"path": "flat.tex", "kind": "update"}]},
    }
    assert summarize_codex_json(event) == "patch: flat.tex"


def test_codex_json_item_agent_message_is_not_an_action() -> None:
    assert summarize_codex_json({"type": "item.completed", "item": {"type": "agent_message", "text": "好了"}}) is None


def test_parse_event_skips_lines_that_are_not_json_objects() -> None:
    assert parse_event(b"not json at all\n") is None
    assert parse_event(b'["a", "b"]\n') is None
    assert parse_event(b"\xff\xfe\n") is None
    assert parse_event(b"") is None


def test_parse_event_returns_the_object() -> None:
    event = parse_event(stream_line(tool_use("Bash", {"command": "ls"})) + b"\n")
    assert event is not None
    assert summarize_stream_json(event) == "Bash: ls"


def pi_tool(name: str, arguments: object) -> dict:
    return {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": name, "args": arguments}


def test_pi_json_bash_shows_the_command() -> None:
    assert (
        summarize_pi_json(pi_tool("bash", {"command": "latexmk -xelatex flat.tex"}))
        == "bash: latexmk -xelatex flat.tex"
    )


def test_pi_json_file_tools_show_the_path() -> None:
    for name in ("read", "edit", "write"):
        assert summarize_pi_json(pi_tool(name, {"path": "flat.tex", "content": "x"})) == f"{name}: flat.tex"


def test_pi_json_search_tools_show_the_pattern_or_path() -> None:
    assert summarize_pi_json(pi_tool("grep", {"pattern": "usepackage", "path": "."})) == "grep: usepackage"
    assert summarize_pi_json(pi_tool("find", {"pattern": "**/*.tex"})) == "find: **/*.tex"
    assert summarize_pi_json(pi_tool("ls", {"path": "figures"})) == "ls: figures"


def test_pi_json_unknown_tool_or_missing_argument_shows_the_name_only() -> None:
    assert summarize_pi_json(pi_tool("ls", {})) == "ls"
    assert summarize_pi_json(pi_tool("custom", {"x": 1})) == "custom"
    assert summarize_pi_json(pi_tool("bash", "not a dict")) == "bash"


def test_pi_json_newlines_collapse_to_one_line() -> None:
    assert summarize_pi_json(pi_tool("bash", {"command": "ls\n  -la"})) == "bash: ls -la"


def test_pi_json_other_event_types_are_not_actions() -> None:
    for event in (
        {"type": "tool_execution_end", "toolName": "bash"},
        {"type": "message_end", "message": {"stopReason": "stop"}},
        {"type": "tool_execution_start"},
    ):
        assert summarize_pi_json(event) is None


def test_codex_actions_are_not_repeated_at_completion() -> None:
    for item in (
        {"type": "command_execution", "command": "latexmk flat.tex"},
        {"type": "file_change", "changes": [{"path": "flat.tex"}]},
    ):
        actions = [
            summarize_codex_json({"type": event, "item": item})
            for event in ("item.started", "item.updated", "item.completed")
        ]
        assert sum(action is not None for action in actions) == 1
