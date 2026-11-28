from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pymupdf
import pytest

from tongtu.model.config import OmlxConfig
from tongtu.reconstruct import omlx

KEY = "sk-FAKE-SECRET-1"

CONFIG = OmlxConfig(enabled=True, base_url="http://127.0.0.1:11000/v1/", api_key=KEY)

SHOWN = "http://127.0.0.1:11000/v1"


def answer(status_code: int, body: object) -> httpx.Response:
    request = httpx.Request("GET", "http://127.0.0.1:11000/v1/models")
    if isinstance(body, str):
        return httpx.Response(status_code, text=body, request=request)
    return httpx.Response(status_code, json=body, request=request)


def wire(monkeypatch: pytest.MonkeyPatch, method: str, response: httpx.Response | Exception) -> list[dict]:
    calls: list[dict] = []

    def send(url: str, **kwargs: object) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(omlx.httpx, method, send)
    return calls


def models(*ids: str) -> dict:
    return {"object": "list", "data": [{"id": model_id, "object": "model"} for model_id in ids]}


def completion(content: object) -> dict:
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}


def test_check_finds_the_model_by_its_directory_name(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire(monkeypatch, "get", answer(200, models("Qwen3-VL", "TeleOCR-oQ8-MLX")))
    assert omlx.check(CONFIG) == ("TeleOCR-oQ8-MLX", "")
    assert calls == [
        {
            "url": "http://127.0.0.1:11000/v1/models",
            "headers": {"Authorization": f"Bearer {KEY}"},
            "timeout": omlx.OMLX_CHECK_TIMEOUT_SECONDS,
        }
    ]
    assert omlx.OMLX_CHECK_TIMEOUT_SECONDS == 10


def test_check_prefers_the_full_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, "get", answer(200, models("TeleOCR-oQ8-MLX", "groxaxo/TeleOCR-oQ8-MLX")))
    assert omlx.check(CONFIG) == ("groxaxo/TeleOCR-oQ8-MLX", "")


def test_check_without_a_key_sends_no_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire(monkeypatch, "get", answer(200, models("TeleOCR-oQ8-MLX")))
    assert omlx.check(OmlxConfig(enabled=True)) == ("TeleOCR-oQ8-MLX", "")
    assert calls[0]["url"] == "http://127.0.0.1:8000/v1/models"
    assert calls[0]["headers"] == {}


@pytest.mark.parametrize(
    ("ids", "model", "matched"),
    [
        (["groxaxo/TeleOCR-oQ8-MLX"], "groxaxo/TeleOCR-oQ8-MLX", "groxaxo/TeleOCR-oQ8-MLX"),
        (["TeleOCR-oQ8-MLX"], "groxaxo/TeleOCR-oQ8-MLX", "TeleOCR-oQ8-MLX"),
        (["TeleOCR-oQ8-MLX"], "TeleOCR-oQ8-MLX", "TeleOCR-oQ8-MLX"),
        (["oQ8-MLX"], "groxaxo/TeleOCR-oQ8-MLX", ""),
        (["other/TeleOCR-oQ8-MLX"], "groxaxo/TeleOCR-oQ8-MLX", ""),
        ([], "groxaxo/TeleOCR-oQ8-MLX", ""),
    ],
)
def test_matched_model(ids: list[str], model: str, matched: str) -> None:
    assert omlx.matched_model(ids, model) == matched


@pytest.mark.parametrize("error", [httpx.ConnectError("connection refused"), httpx.ReadTimeout("timed out")])
def test_check_reports_an_unreachable_server_with_the_commands(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    wire(monkeypatch, "get", error)
    model_id, problem = omlx.check(CONFIG)
    assert model_id == ""
    assert problem == (
        f"cannot reach oMLX at {SHOWN} ({type(error).__name__}). Start it with:\n"
        "  omlx start\n"
        "or, for a source install:\n"
        "  omlx serve --port 11000"
    )


@pytest.mark.parametrize("status_code", [401, 403])
def test_check_reports_a_refused_key(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    wire(monkeypatch, "get", answer(status_code, {"error": f"bad key {KEY}"}))
    model_id, problem = omlx.check(CONFIG)
    assert model_id == ""
    assert f"answered HTTP {status_code}" in problem
    assert "tongtu config set omlx.api_key <key>" in problem
    assert KEY not in problem


def test_check_reports_another_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, "get", answer(500, "server error"))
    assert omlx.check(CONFIG) == ("", f"oMLX at {SHOWN} answered HTTP 500")


@pytest.mark.parametrize("body", ["not json", {"models": ["TeleOCR-oQ8-MLX"]}, {"data": [{"name": "x"}]}])
def test_check_reports_an_answer_that_is_not_a_model_list(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    wire(monkeypatch, "get", answer(200, body))
    assert omlx.check(CONFIG) == ("", f"the answer of {SHOWN}/models is not an OpenAI-compatible model list")


def test_check_reports_a_missing_model_with_the_download_command(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, "get", answer(200, models("Qwen3-VL", "PaddleOCR-VL")))
    model_id, problem = omlx.check(CONFIG)
    assert model_id == ""
    assert problem == (
        f"oMLX at {SHOWN} has no model groxaxo/TeleOCR-oQ8-MLX; it lists Qwen3-VL, PaddleOCR-VL. Download it with:\n"
        "  uvx --from huggingface_hub hf download groxaxo/TeleOCR-oQ8-MLX "
        "--local-dir ~/.omlx/models/groxaxo/TeleOCR-oQ8-MLX\n"
        "  omlx restart"
    )


def test_check_reports_an_empty_model_list(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, "get", answer(200, models()))
    assert "it lists no model." in omlx.check(CONFIG)[1]


def test_check_shows_only_scheme_host_port_and_path(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, "get", httpx.ConnectError("refused"))
    config = CONFIG.model_copy(update={"base_url": "http://FAKE-USER:FAKE-PASS@localhost/v1?token=FAKE-TOKEN"})
    problem = omlx.check(config)[1]
    assert problem.startswith("cannot reach oMLX at http://localhost:80/v1 (ConnectError).")
    assert "omlx serve --port 80" in problem
    assert "FAKE" not in problem


def test_check_reports_a_malformed_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    problem = omlx.check(CONFIG.model_copy(update={"base_url": "not a url"}))[1]
    assert problem.startswith("cannot reach oMLX at omlx.base_url (")
    assert KEY not in problem


def test_check_does_not_send_a_key_that_cannot_go_into_a_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire(monkeypatch, "get", answer(200, models("TeleOCR-oQ8-MLX")))
    config = CONFIG.model_copy(update={"api_key": "sk-FAKE-SECRET-é"})
    assert omlx.check(config) == ("", "omlx.api_key has characters that cannot be sent in a request header")
    assert calls == []


def test_transcribe_sends_the_teleocr_formula_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire(monkeypatch, "post", answer(200, completion("x^2<|im_end|>")))
    assert omlx.transcribe(CONFIG, "TeleOCR-oQ8-MLX", b"PNG") == ("x^2<|im_end|>", "")
    assert calls == [
        {
            "url": "http://127.0.0.1:11000/v1/chat/completions",
            "headers": {"Authorization": f"Bearer {KEY}"},
            "timeout": 120,
            "json": {
                "model": "TeleOCR-oQ8-MLX",
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "UE5H"}},
                            {
                                "type": "text",
                                "text": "\nPlease write out the expression of the formula in the image using LaTeX format.",
                            },
                        ],
                    },
                ],
                "temperature": 0,
                "max_tokens": 768,
            },
        }
    ]
    assert base64.b64decode("UE5H") == b"PNG"


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (httpx.ConnectError("refused"), "ConnectError"),
        (httpx.ReadTimeout("timed out"), "ReadTimeout"),
        (answer(500, "server error"), "HTTP 500"),
        (answer(200, "not json"), "the answer is not a chat completion"),
        (answer(200, {"choices": []}), "the answer has no text"),
        (answer(200, completion(None)), "the answer has no text"),
        (answer(200, completion("  ")), "the answer has no text"),
    ],
)
def test_transcribe_reports_a_failed_request(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception, problem: str
) -> None:
    wire(monkeypatch, "post", response)
    assert omlx.transcribe(CONFIG, "TeleOCR-oQ8-MLX", b"PNG") == ("", problem)


def write_png(path: Path, width: int, height: int) -> Path:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pixmap.save(path)
    return path


def test_transcribe_all_sends_the_largest_images_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    images = {
        "i-1": write_png(tmp_path / "i-1.png", 10, 10),
        "e-1": write_png(tmp_path / "e-1.png", 80, 30),
        "i-2": write_png(tmp_path / "i-2.png", 20, 10),
    }
    sizes = {path.read_bytes(): key for key, path in images.items()}
    sent: list[str] = []
    progress: list[int] = []

    def transcribe(config: OmlxConfig, model_id: str, png: bytes) -> tuple[str, str]:
        assert (config, model_id) == (CONFIG, "TeleOCR-oQ8-MLX")
        key = sizes[png]
        sent.append(key)
        return ("", "HTTP 500") if key == "i-2" else (f"latex {key}", "")

    monkeypatch.setattr(omlx, "OMLX_CONCURRENCY", 1)
    monkeypatch.setattr(omlx, "transcribe", transcribe)
    replies, failures = omlx.transcribe_all(CONFIG, "TeleOCR-oQ8-MLX", images, progress.append)
    assert sent == ["e-1", "i-2", "i-1"]
    assert progress == [1, 2, 3]
    assert replies == {"i-1": "latex i-1", "e-1": "latex e-1"}
    assert failures == {"i-2": "HTTP 500"}


def test_concurrency_constant() -> None:
    assert omlx.OMLX_CONCURRENCY == 4
    assert omlx.OMLX_TIMEOUT_SECONDS == 120


def test_failure_warning_names_the_count_and_the_first_problem() -> None:
    warning = omlx.failure_warning({"i-2": "ReadTimeout", "i-5": "HTTP 500"}, 9)
    assert warning == (
        "oMLX could not transcribe 2 of 9 formulas (first: i-2, ReadTimeout); "
        "they go to the reconstruct_formula session"
    )


@pytest.mark.parametrize(
    ("reply", "latex"),
    [
        ("x^2<|im_end|>", "x^2"),
        ("x^2<|im_start|>assistant\nmore", "x^2"),
        ("```latex\n\\[a+b\\]\n```", "a+b"),
        ("$$\\frac{a}{b}$$", "\\frac{a}{b}"),
        ("\\(x_i\\)", "x_i"),
        ("$y$", "y"),
        ("$", "$"),
        ("$$a$$\n$$b$$", "a \\\\ b"),
        ("\\begin{equation}y=f(x)\\tag{3}\\end{equation}", "y=f(x) \\quad (3)"),
        ("a=b \\tag*{(2a)}", "a=b  \\quad (2a)"),
        ("\\begin{align*}a&=b\\\\c&=d\\end{align*}", "\\begin{aligned}a&=b\\\\c&=d\\end{aligned}"),
        ("\\begin{eqnarray}a&=&b\\end{eqnarray}", "\\begin{aligned}a&=&b\\end{aligned}"),
    ],
)
def test_normalized_formula(reply: str, latex: str) -> None:
    assert omlx.normalized_formula(reply) == latex


@pytest.mark.parametrize(
    ("latex", "plain"),
    [
        ("\\left( x \\right)", "( x )"),
        ("\\bigl[ a \\bigr]", "[ a ]"),
        ("\\Big\\{ a \\Big\\}", "\\{ a \\}"),
        ("\\big| a \\big|", "| a |"),
        ("\\left. x \\right|", " x |"),
        ("y_i \\right.", "y_i "),
        ("\\leftarrow \\rightarrow", "\\leftarrow \\rightarrow"),
        ("\\big/ x", "\\big/ x"),
    ],
)
def test_without_delimiter_sizes(latex: str, plain: str) -> None:
    assert omlx.without_delimiter_sizes(latex) == plain
