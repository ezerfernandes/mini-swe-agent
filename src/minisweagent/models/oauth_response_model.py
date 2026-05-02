"""OAuth-authenticated LiteLLM Responses-API model.

Combines the OAuth header / api-base / api-key injection from
:class:`minisweagent.models.oauth_model.OAuthLitellmModel` with the Responses
API request shape used by :class:`LitellmResponseModel`.

Required for the ChatGPT Plus/Pro Codex backend, which only mounts newer
GPT-5 family ids (``gpt-5``, ``gpt-5.1``, ``gpt-5.4``, ...) under
``/responses`` — never under ``/chat/completions``. Pi-mono's
``openai-codex-responses`` provider uses the same shape; see
``pi-mono/packages/ai/src/providers/openai-codex-responses.ts`` for the
reference implementation.

Example::

    model_class: oauth_response
    model_name: openai/gpt-5.4
    oauth_provider: openai-codex
"""

from __future__ import annotations

import json
import logging
import os
import sys
import warnings
from typing import Any, Iterable

import litellm

# LiteLLM's Responses-API types use a tagged union for ``output`` items and a
# strict shape for ``usage``. The Codex backend's actual payloads don't match
# every union member exactly, so every ``model_dump()`` call emits a wall of
# UserWarnings from pydantic. They're advisory — the response still flows
# through correctly — but they drown the console. Silence here, scoped to the
# specific message so genuine pydantic warnings still surface.
for _msg_re in (r"Pydantic serializer warnings:.*", r"PydanticSerializationUnexpectedValue.*"):
    warnings.filterwarnings("ignore", message=_msg_re, category=UserWarning)
del _msg_re

from minisweagent.models.litellm_response_model import (
    LitellmResponseModel,
    LitellmResponseModelConfig,
)
from minisweagent.models.oauth_model import (
    CLAUDE_CODE_SYSTEM_PROMPT,
    OAuthLitellmModelConfig,
    _VALID_PROVIDERS,
    _extract_text,
    resolve_oauth_kwargs,
)
from minisweagent.models.utils.actions_toolcall_response import BASH_TOOL_RESPONSE_API


class OAuthLitellmResponseModelConfig(OAuthLitellmModelConfig, LitellmResponseModelConfig):
    """OAuth fields ``+`` Responses-API fields. Diamond resolves to a single
    ``LitellmModelConfig`` base since both parents inherit from it."""


def _collect_codex_stream(stream: Iterable[Any]) -> Any:
    """Drain a Responses-API stream and return the final ``ResponsesAPIResponse``.

    The Codex backend rejects ``stream: false`` (``"Stream must be set to true"``),
    so for ChatGPT-account requests we always stream and re-aggregate here.

    The Codex SSE variant carries an *empty* ``output`` array on its
    ``response.completed`` event — the actual function_call / message /
    reasoning items arrive on ``response.output_item.done`` events and must
    be reassembled into ``final.output`` for downstream parsing. Pi-mono's
    ``processResponsesStream`` does the same walk.
    """
    final: Any = None
    last: Any = None
    items_done: list = []
    for event in stream:
        last = event
        evt_type = getattr(event, "type", None)
        if evt_type == "response.output_item.done":
            item = getattr(event, "item", None)
            if item is not None:
                items_done.append(item)
        elif evt_type == "response.completed":
            final = getattr(event, "response", None) or final
        elif evt_type == "response.incomplete":
            logging.getLogger("oauth_response_model").warning(
                "Codex Responses API: stream ended with response.incomplete — output may be truncated"
            )
            final = getattr(event, "response", None) or final
        elif evt_type == "response.failed":
            err = getattr(event, "response", None)
            raise RuntimeError(f"Codex Responses API: response.failed event received: {err!r}")
    if final is None:
        # LiteLLM's iterator also stashes the last completed response on the
        # iterator object itself; fall back to that before giving up.
        final = getattr(stream, "completed_response", None) or getattr(last, "response", None)
    if final is None:
        raise RuntimeError("Codex Responses API: stream ended without a response.completed event")
    # If the server-sent completed event carries fewer items than we observed
    # via output_item.done (Codex backend always sends []), trust the per-item
    # stream — those events are the source of truth.
    if items_done:
        existing = getattr(final, "output", None) or []
        if len(items_done) > len(existing):
            try:
                final.output = items_done  # type: ignore[attr-defined]
            except Exception:
                try:
                    final = final.model_copy(update={"output": items_done})
                except Exception as exc:
                    raise RuntimeError(
                        f"Codex Responses API: {len(items_done)} output items collected from stream "
                        f"but could not be stitched onto the response object — tool calls will be lost. "
                        f"Set MSWEA_OAUTH_RESPONSE_DEBUG=1 for the full payload."
                    ) from exc
    if os.getenv("MSWEA_OAUTH_RESPONSE_DEBUG"):
        try:
            dump = final.model_dump() if hasattr(final, "model_dump") else dict(final)
        except Exception as exc:
            dump = {"_dump_error": repr(exc), "_repr": repr(final)}
        sys.stderr.write(
            "[oauth_response] aggregated response payload:\n"
            + json.dumps(dump, indent=2, default=str)
            + "\n"
        )
        sys.stderr.flush()
    return final


def _split_system_message(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Pop a leading system message and return ``(instructions_text, remaining)``.

    Codex's Responses API takes the system prompt as a top-level
    ``instructions`` field rather than a message — pi-mono does the same in
    ``convertResponsesMessages({ includeSystemPrompt: false })``.
    """
    instructions: str | None = None
    remaining: list[dict] = []
    seen_system = False
    for msg in messages:
        if not seen_system and msg.get("role") == "system":
            instructions = _extract_text(msg.get("content"))
            seen_system = True
            continue
        remaining.append(msg)
    return instructions, remaining


class OAuthLitellmResponseModel(LitellmResponseModel):
    """LiteLLM Responses-API model that authenticates via stored OAuth credentials."""

    def __init__(self, **kwargs: Any) -> None:
        provider = kwargs.get("oauth_provider")
        if provider not in _VALID_PROVIDERS:
            raise ValueError(f"oauth_provider must be one of {sorted(_VALID_PROVIDERS)}, got {provider!r}")
        super().__init__(config_class=OAuthLitellmResponseModelConfig, **kwargs)
        self.config: OAuthLitellmResponseModelConfig

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        prepared = super()._prepare_messages_for_api(messages)
        if self.config.oauth_provider == "anthropic" and self.config.inject_claude_code_system:
            already_present = any(
                msg.get("role") == "system" and CLAUDE_CODE_SYSTEM_PROMPT in _extract_text(msg.get("content"))
                for msg in prepared
            )
            if not already_present:
                prepared = [{"role": "system", "content": CLAUDE_CODE_SYSTEM_PROMPT}, *prepared]
        return prepared

    def _query(self, messages: list[dict[str, str]], **kwargs):
        oauth_kwargs = resolve_oauth_kwargs(self.config)
        # OAuth identity (api_key, api_base, Authorization, etc.) MUST win over
        # caller kwargs — see OAuthLitellmModel._query for the full rationale.
        merged = {**kwargs, **oauth_kwargs}
        config_headers = self.config.model_kwargs.get("extra_headers") or {}
        caller_headers = kwargs.get("extra_headers") or {}
        oauth_headers = oauth_kwargs.get("extra_headers") or {}
        if config_headers or caller_headers or oauth_headers:
            merged["extra_headers"] = {**config_headers, **caller_headers, **oauth_headers}

        if self.config.oauth_provider == "openai-codex":
            return self._codex_query(messages, merged)

        return super()._query(messages, **merged)

    def _codex_query(self, messages: list[dict], merged: dict[str, Any]):
        """ChatGPT-account Codex Responses path.

        Mirrors pi-mono's ``buildRequestBody`` / ``streamOpenAICodexResponses``:
        always streams (server rejects ``stream: false``), passes the system
        prompt as ``instructions``, sets ``store: false``, and explicitly
        opts in to tool calling. Reasoning models additionally need a
        ``reasoning`` block — without it the model often replies in prose and
        never emits ``function_call`` items.
        """
        instructions, messages = _split_system_message(messages)
        # `strict: False` matches pi-mono's `convertResponsesTools(..., {strict: null})`
        # — the Codex backend rejects strict-schema validation for our flexible
        # bash arguments shape.
        tools = [{**BASH_TOOL_RESPONSE_API, "strict": False}]
        body: dict[str, Any] = {
            "store": False,
            "stream": True,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": "medium", "summary": "auto"},
        }
        if instructions is not None:
            body["instructions"] = instructions
        # Caller / config kwargs win over our defaults; OAuth identity wins over
        # everything (already merged in by _query).
        body.update(self.config.model_kwargs)
        body.update(merged)
        # Remove keys that are passed as explicit positional kwargs to litellm.responses
        # so we don't get "multiple values for keyword argument" TypeError.
        body.pop("model", None)
        body.pop("input", None)
        body.pop("tools", None)
        try:
            stream = litellm.responses(
                model=self.config.model_name,
                input=messages,
                tools=tools,
                **body,
            )
        except litellm.exceptions.AuthenticationError as e:
            e.message += " You can permanently set your API key with `mini-extra config set KEY VALUE`."
            raise
        return _collect_codex_stream(stream)


__all__ = [
    "OAuthLitellmResponseModel",
    "OAuthLitellmResponseModelConfig",
    "_collect_codex_stream",
    "_split_system_message",
]
