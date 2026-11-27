from __future__ import annotations

import pytest

from tongtu.reconstruct import latex


def test_tool_output_returns_the_standard_output() -> None:
    assert latex.tool_output(["echo", "found"]).strip() == "found"


def test_tool_output_of_a_missing_command_names_the_command() -> None:
    with pytest.raises(latex.ToolError, match="the command tongtu-missing-command was not found"):
        latex.tool_output(["tongtu-missing-command"])


def test_tool_output_of_a_command_that_does_not_finish_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(latex, "TOOL_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(latex.ToolError, match="sleep 5 did not finish within 0.2 seconds"):
        latex.tool_output(["sleep", "5"])
