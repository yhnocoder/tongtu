from __future__ import annotations

import base64
import re
import struct
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from ..artifacts.reconstruct import RegionKind
from ..model.chat import Completion, reply_text, sendable_in_header
from ..model.config import OmlxConfig
from . import formula
from .recognition import Verdict

OMLX_CHECK_TIMEOUT_SECONDS = 10

OMLX_TIMEOUT_SECONDS = 120

OMLX_CONCURRENCY = 4

SYSTEM_PROMPT = "You are a helpful assistant."

FORMULA_PROMPT = "\nPlease write out the expression of the formula in the image using LaTeX format."

FORMULA_TEMPERATURE = 0

FORMULA_MAX_TOKENS = 768

DEFAULT_PORTS = {"http": 80, "https": 443}

UNKNOWN_URL = "omlx.base_url"

PNG_SIZE_SLICE = slice(16, 24)

END_TOKEN_RE = re.compile(r"<\|im_end\|>|<\|endoftext\|>|</s>|<\|end\|>|<\|user\|>|<eos>|<\|im_start\|>.*", re.S)

CODE_FENCE_RE = re.compile(r"^```(?:latex)?\s*|\s*```$")

MATH_DELIMITERS = (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$"))

DISPLAY_JOIN_RE = re.compile(r"\$\$\s*\n?\s*\$\$")

TAG_RE = re.compile(r"\\tag\*?\s*\{\s*\(?([^{}()]+?)\)?\s*\}")

NUMBERED_ENVIRONMENT_RE = re.compile(r"\\(begin|end)\{(align\*?|equation\*?|eqnarray\*?)\}")

NULL_DELIMITER_RE = re.compile(r"\\(left|right)\s*\.")

DELIMITER_SIZE_RE = re.compile(r"\\(left|right|bigl|bigr|Bigl|Bigr|big|Big)\s*(?=[()\[\]|.]|\\[{}|])")


class _ModelEntry(BaseModel):
    id: str


class _ModelList(BaseModel):
    data: list[_ModelEntry]


def endpoint(base_url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(base_url)
        port = parts.port or DEFAULT_PORTS.get(parts.scheme)
    except ValueError:
        return UNKNOWN_URL, "PORT"
    if not parts.hostname or port is None:
        return UNKNOWN_URL, "PORT"
    return f"{parts.scheme}://{parts.hostname}:{port}{parts.path.rstrip('/')}", str(port)


def _headers(config: OmlxConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}


def _url(config: OmlxConfig, path: str) -> str:
    return f"{config.base_url.rstrip('/')}/{path}"


def matched_model(ids: list[str], model: str) -> str:
    for wanted in (model, model.rsplit("/", 1)[-1]):
        if wanted in ids:
            return wanted
    return ""


def unreachable_message(shown: str, port: str, error: str) -> str:
    return (
        f"cannot reach oMLX at {shown} ({error}). Start it with:\n"
        "  omlx start\n"
        "or, for a source install:\n"
        f"  omlx serve --port {port}"
    )


def missing_model_message(shown: str, model: str, ids: list[str]) -> str:
    listed = f"it lists {', '.join(ids)}" if ids else "it lists no model"
    return (
        f"oMLX at {shown} has no model {model}; {listed}. Download it with:\n"
        f"  uvx --from huggingface_hub hf download {model} --local-dir ~/.omlx/models/{model}\n"
        "  omlx restart"
    )


def check(config: OmlxConfig) -> tuple[str, str]:
    shown, port = endpoint(config.base_url)
    if config.api_key and not sendable_in_header(config.api_key):
        return "", "omlx.api_key has characters that cannot be sent in a request header"
    try:
        response = httpx.get(_url(config, "models"), headers=_headers(config), timeout=OMLX_CHECK_TIMEOUT_SECONDS)
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        return "", unreachable_message(shown, port, type(error).__name__)
    if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return "", (
            f"oMLX at {shown} answered HTTP {response.status_code}. Set the API key that oMLX expects with:\n"
            "  tongtu config set omlx.api_key <key>"
        )
    if not response.is_success:
        return "", f"oMLX at {shown} answered HTTP {response.status_code}"
    try:
        ids = [entry.id for entry in _ModelList.model_validate_json(response.content).data]
    except ValidationError:
        return "", f"the answer of {shown}/models is not an OpenAI-compatible model list"
    model_id = matched_model(ids, config.model)
    if not model_id:
        return "", missing_model_message(shown, config.model, ids)
    return model_id, ""


def transcribe(config: OmlxConfig, model_id: str, png: bytes) -> tuple[str, str]:
    image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image}},
                    {"type": "text", "text": FORMULA_PROMPT},
                ],
            },
        ],
        "temperature": FORMULA_TEMPERATURE,
        "max_tokens": FORMULA_MAX_TOKENS,
    }
    try:
        response = httpx.post(
            _url(config, "chat/completions"), json=body, headers=_headers(config), timeout=OMLX_TIMEOUT_SECONDS
        )
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        return "", type(error).__name__
    if not response.is_success:
        return "", f"HTTP {response.status_code}"
    try:
        reply = reply_text(Completion.model_validate_json(response.content))
    except ValidationError:
        return "", "the answer is not a chat completion"
    if not reply:
        return "", "the answer has no text"
    return reply, ""


def png_area(png: bytes) -> int:
    width, height = struct.unpack(">II", png[PNG_SIZE_SLICE])
    return width * height


def transcribe_all(
    config: OmlxConfig, model_id: str, images: Mapping[str, Path], progress: Callable[[int], None]
) -> tuple[dict[str, str], dict[str, str]]:
    pngs = {key: path.read_bytes() for key, path in images.items()}
    order = sorted(pngs, key=lambda key: -png_area(pngs[key]))
    lock = threading.Lock()
    done = 0

    def one(key: str) -> tuple[str, str]:
        nonlocal done
        outcome = transcribe(config, model_id, pngs[key])
        with lock:
            done += 1
            progress(done)
        return outcome

    with ThreadPoolExecutor(OMLX_CONCURRENCY) as pool:
        outcomes = dict(zip(order, pool.map(one, order), strict=True))
    replies = {key: reply for key in images if (reply := outcomes[key][0])}
    failures = {key: problem for key in images if (problem := outcomes[key][1])}
    return replies, failures


def failure_warning(failures: Mapping[str, str], total: int) -> str:
    key, problem = next(iter(failures.items()))
    return (
        f"oMLX could not transcribe {len(failures)} of {total} formulas (first: {key}, {problem}); "
        "they go to the reconstruct_formula session"
    )


def normalized_formula(text: str) -> str:
    text = END_TOKEN_RE.sub("", text).strip()
    text = CODE_FENCE_RE.sub("", text).strip()
    for opening, closing in MATH_DELIMITERS:
        if text.startswith(opening) and text.endswith(closing) and len(text) >= len(opening) + len(closing):
            text = text[len(opening) : len(text) - len(closing)].strip()
            break
    text = DISPLAY_JOIN_RE.sub(r" \\\\ ", text)
    text = TAG_RE.sub(r" \\quad (\1)", text)
    text = NUMBERED_ENVIRONMENT_RE.sub(
        lambda match: "" if match.group(2).startswith("equation") else f"\\{match.group(1)}{{aligned}}", text
    )
    return text.strip()


def without_delimiter_sizes(latex: str) -> str:
    return DELIMITER_SIZE_RE.sub("", NULL_DELIMITER_RE.sub("", latex))


def verify_replies(tree: Path, replies: Mapping[str, str], keys: Mapping[str, RegionKind]) -> dict[str, Verdict]:
    answered = {key: kind for key, kind in keys.items() if key in replies}
    latex = {key: normalized_formula(replies[key]) for key in answered}
    verdicts = formula.verify_all(tree, latex, answered)
    retried = {
        key: plain
        for key in answered
        if not verdicts[key].passed and (plain := without_delimiter_sizes(latex[key])) != latex[key]
    }
    if retried:
        second = formula.verify_all(tree, retried, {key: keys[key] for key in retried})
        verdicts |= {key: verdict for key, verdict in second.items() if verdict.passed}
    return verdicts
