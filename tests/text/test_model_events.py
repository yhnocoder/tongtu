from __future__ import annotations

import json

from tongtu.model.events import SUMMARIZERS, summarize_codex_json, summarize_stream_json, summarizer


def stream_line(*blocks: dict) -> bytes:
    return json.dumps({"type": "assistant", "message": {"content": list(blocks)}}).encode("utf-8")


def tool_use(name: str, arguments: dict) -> dict:
    return {"type": "tool_use", "id": "toolu_01", "name": name, "input": arguments}


def test_registry_holds_both_formats() -> None:
    assert set(SUMMARIZERS) == {"stream-json", "codex-json"}


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


def test_codex_json_exec_command_begin_shows_the_command() -> None:
    event = {"id": "0", "msg": {"type": "exec_command_begin", "command": ["bash", "-lc", "latexmk flat.tex"]}}
    assert summarize_codex_json(event) == "exec: bash -lc latexmk flat.tex"


def test_codex_json_patch_apply_begin_shows_the_files() -> None:
    event = {"id": "1", "msg": {"type": "patch_apply_begin", "changes": {"flat.tex": {"update": {}}}}}
    assert summarize_codex_json(event) == "patch: flat.tex"


def test_codex_json_other_msg_types_are_not_actions() -> None:
    for kind in ("agent_message", "token_count", "task_started"):
        assert summarize_codex_json({"id": "2", "msg": {"type": kind}}) is None


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


def test_summarizer_without_events_is_absent() -> None:
    assert summarizer(None) is None


def test_summarizer_skips_lines_that_are_not_json_objects() -> None:
    summarize = summarizer("stream-json")
    assert summarize is not None
    assert summarize(b"not json at all\n") is None
    assert summarize(b'["a", "b"]\n') is None
    assert summarize(b"\xff\xfe\n") is None


def test_summarizer_parses_a_stream_json_line() -> None:
    summarize = summarizer("stream-json")
    assert summarize is not None
    assert summarize(stream_line(tool_use("Bash", {"command": "ls"})) + b"\n") == "Bash: ls"
