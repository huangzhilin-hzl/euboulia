"""Fail-closed smoke correctness checks for an owned SGLang endpoint."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

_DEFAULT_PROMPT: Final[str] = "Reply with the single word OK."


class CorrectnessHarnessError(RuntimeError):
    """Raised when the endpoint fails the declared smoke contract."""


@dataclass(frozen=True, slots=True)
class SmokeSettings:
    """Normalized smoke-test inputs sourced from Euboulia's environment."""

    endpoint: str
    model: str
    api: str
    prompt: str
    max_tokens: int
    requests: int
    timeout_seconds: float

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> SmokeSettings:
        values = os.environ if environ is None else environ
        endpoint = _required(values, "EUBOULIA_TARGET_ENDPOINT")
        model = values.get("EUBOULIA_MODEL_SERVED_NAME") or _required(values, "EUBOULIA_MODEL")
        api = values.get("EUBOULIA_SMOKE_API", "native").strip().casefold()
        if api not in {"native", "openai-chat"}:
            raise CorrectnessHarnessError("EUBOULIA_SMOKE_API must be 'native' or 'openai-chat'")
        return cls(
            endpoint=_base_endpoint(endpoint),
            model=model,
            api=api,
            prompt=values.get("EUBOULIA_SMOKE_PROMPT", _DEFAULT_PROMPT),
            max_tokens=_positive_int(values, "EUBOULIA_SMOKE_MAX_TOKENS", 8),
            requests=_positive_int(values, "EUBOULIA_SMOKE_REQUESTS", 1),
            timeout_seconds=_positive_float(values, "EUBOULIA_SMOKE_TIMEOUT_SECONDS", 120.0),
        )


def run_smoke(settings: SmokeSettings) -> dict[str, object]:
    """Run every declared smoke request and return a compact summary."""

    request_url, payload = _request(settings)
    completed = 0
    for index in range(settings.requests):
        response = _post_json(request_url, payload, settings.timeout_seconds)
        try:
            _validate_response(response, settings.api)
        except CorrectnessHarnessError as exc:
            raise CorrectnessHarnessError(f"smoke request {index + 1} failed: {exc}") from exc
        completed += 1
    return {
        "api": settings.api,
        "completed": completed,
        "endpoint": settings.endpoint,
        "model": settings.model,
    }


def _request(settings: SmokeSettings) -> tuple[str, dict[str, object]]:
    if settings.api == "native":
        return (
            f"{settings.endpoint}/generate",
            {
                "text": settings.prompt,
                "sampling_params": {
                    "temperature": 0,
                    "max_new_tokens": settings.max_tokens,
                },
            },
        )
    return (
        f"{settings.endpoint}/v1/chat/completions",
        {
            "model": settings.model,
            "messages": [{"role": "user", "content": settings.prompt}],
            "temperature": 0,
            "max_tokens": settings.max_tokens,
        },
    )


def _post_json(url: str, payload: Mapping[str, object], timeout_seconds: float) -> object:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise CorrectnessHarnessError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise CorrectnessHarnessError(f"request failed: {exc.reason}") from exc
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorrectnessHarnessError("response is not UTF-8 JSON") from exc


def _validate_response(value: object, api: str) -> None:
    if not isinstance(value, dict):
        raise CorrectnessHarnessError("response must be a JSON object")
    response = cast(dict[object, object], value)
    if api == "native":
        text = response.get("text")
        if isinstance(text, str) and text.strip():
            return
        if (
            isinstance(text, list)
            and text
            and all(isinstance(item, str) and item.strip() for item in text)
        ):
            return
        raise CorrectnessHarnessError("native response contains no generated text")

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise CorrectnessHarnessError("OpenAI response contains no choices")
    message = cast(dict[object, object], choices[0]).get("message")
    if not isinstance(message, dict):
        raise CorrectnessHarnessError("OpenAI response contains no assistant message")
    assistant = cast(dict[object, object], message)
    for field in ("content", "reasoning_content"):
        content = assistant.get(field)
        if isinstance(content, str) and content.strip():
            return
    tool_calls = assistant.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return
    raise CorrectnessHarnessError("assistant message has no content, reasoning, or tool calls")


def _base_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CorrectnessHarnessError("EUBOULIA_TARGET_ENDPOINT must be an HTTP(S) URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise CorrectnessHarnessError(
            "EUBOULIA_TARGET_ENDPOINT must be a base URL without path, query, or fragment"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise CorrectnessHarnessError(f"{name} is required")
    return value


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise CorrectnessHarnessError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise CorrectnessHarnessError(f"{name} must be a positive integer")
    return value


def _positive_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise CorrectnessHarnessError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise CorrectnessHarnessError(f"{name} must be a positive number")
    return value


def main() -> int:
    """CLI entrypoint used by optimization correctness commands."""

    try:
        summary = run_smoke(SmokeSettings.from_environment())
    except CorrectnessHarnessError as exc:
        print(f"SGLang correctness failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main tests
    raise SystemExit(main())
