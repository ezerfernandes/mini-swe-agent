"""Tests for the Responses-API variant of the OAuth model class."""

from __future__ import annotations

import time

import pytest

from minisweagent import oauth
from minisweagent.oauth.types import (
    OAuthCredentials,
    OAuthLoginCallbacks,
    OAuthProviderInterface,
)


class _StubProvider(OAuthProviderInterface):
    id = "stub-model"
    name = "Stub"

    def __init__(self, account_id: str | None = None) -> None:
        self.refreshes = 0
        self._extra: dict = {}
        if account_id:
            self._extra["account_id"] = account_id

    def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:  # pragma: no cover
        return OAuthCredentials(refresh="r", access="a", expires=0, extra=self._extra)

    def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        self.refreshes += 1
        return OAuthCredentials(
            refresh=credentials.refresh,
            access="fresh-token",
            expires=int(time.time() * 1000) + 60_000,
            extra=self._extra,
        )

    def get_api_key(self, credentials: OAuthCredentials) -> str:  # pragma: no cover
        return credentials.access


@pytest.fixture
def isolated_oauth(monkeypatch, tmp_path):
    monkeypatch.setenv("MSWEA_OAUTH_FILE", str(tmp_path / "oauth.json"))
    return


@pytest.fixture
def register_stub_provider():
    """Register a stub OAuth provider and guarantee teardown even on test failure."""
    registered: list[str] = []

    def _register(provider_id: str, stub: OAuthProviderInterface) -> None:
        stub.id = provider_id  # type: ignore[misc]
        oauth.register_oauth_provider(stub)
        registered.append(provider_id)

    yield _register

    for provider_id in registered:
        oauth.restore_oauth_provider(provider_id)


def test_invalid_provider_rejected(isolated_oauth):
    from minisweagent.models.oauth_response_model import OAuthLitellmResponseModel

    with pytest.raises(ValueError, match="oauth_provider"):
        OAuthLitellmResponseModel(model_name="openai/gpt-5", oauth_provider="bogus")


def test_codex_query_injects_store_false_stream_and_instructions(isolated_oauth, monkeypatch, register_stub_provider):
    """ChatGPT-account Codex backend requires ``stream: true`` and ``store: false``,
    expects the system prompt as a top-level ``instructions`` field rather
    than as a message in ``input``, and needs explicit tool-choice + reasoning
    knobs to actually emit ``function_call`` items."""
    import litellm

    from minisweagent.models.oauth_response_model import OAuthLitellmResponseModel

    sentinel_response = object()

    class _FakeCompletedEvent:
        type = "response.completed"
        response = sentinel_response

    stub = _StubProvider(account_id="acc-42")
    register_stub_provider("openai-codex", stub)
    captured: dict = {}

    def fake_responses(*, model, input, tools, **kwargs):
        captured["model"] = model
        captured["messages"] = input
        captured["tools"] = tools
        captured.update(kwargs)
        return iter([_FakeCompletedEvent()])

    oauth.storage.save(
        "openai-codex",
        OAuthCredentials(refresh="r", access="x", expires=0, extra={"account_id": "acc-42"}),
    )
    model = OAuthLitellmResponseModel(
        model_name="openai/gpt-5.4", oauth_provider="openai-codex"
    )

    monkeypatch.setattr(litellm, "responses", fake_responses)
    result = model._query(
        [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "ping"},
        ]
    )

    assert captured["store"] is False
    assert captured["stream"] is True
    assert captured["tool_choice"] == "auto"
    assert captured["parallel_tool_calls"] is True
    assert captured["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert captured["include"] == ["reasoning.encrypted_content"]
    assert captured["instructions"] == "be helpful"
    assert captured["messages"] == [{"role": "user", "content": "ping"}]
    assert captured["api_base"] == "https://chatgpt.com/backend-api/codex"
    assert captured["extra_headers"]["chatgpt-account-id"] == "acc-42"
    assert captured["extra_headers"]["OpenAI-Beta"] == "responses=experimental"
    assert captured["tools"] == [
        {
            "type": "function",
            "name": "bash",
            "description": "Execute a bash command",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute"}
                },
                "required": ["command"],
            },
            "strict": False,
        }
    ]
    assert result is sentinel_response


def test_collect_codex_stream_picks_completed_event():
    from minisweagent.models.oauth_response_model import _collect_codex_stream

    final = object()

    class _Evt:
        def __init__(self, type_, response=None):
            self.type = type_
            self.response = response

    events = [
        _Evt("response.created"),
        _Evt("response.output_text.delta"),
        _Evt("response.completed", response=final),
    ]
    assert _collect_codex_stream(iter(events)) is final


def test_collect_codex_stream_raises_on_failed_event():
    import pytest

    from minisweagent.models.oauth_response_model import _collect_codex_stream

    class _Evt:
        def __init__(self, type_, response=None):
            self.type = type_
            self.response = response

    events = [_Evt("response.created"), _Evt("response.failed", response={"error": "boom"})]
    with pytest.raises(RuntimeError, match="response.failed"):
        _collect_codex_stream(iter(events))


def test_collect_codex_stream_aggregates_items_when_completed_is_empty():
    """The Codex backend's ``response.completed`` event carries ``output: []``;
    real output items arrive on ``response.output_item.done`` and must be
    stitched onto the final response."""
    from minisweagent.models.oauth_response_model import _collect_codex_stream

    class _Evt:
        def __init__(self, type_, item=None, response=None):
            self.type = type_
            self.item = item
            self.response = response

    class _Item:
        def __init__(self, type_, name=None, args=None, call_id=None):
            self.type = type_
            self.name = name
            self.arguments = args
            self.call_id = call_id

        def model_dump(self):
            return {
                "type": self.type,
                "name": self.name,
                "arguments": self.arguments,
                "call_id": self.call_id,
            }

    class _FinalResponse:
        def __init__(self):
            self.output: list = []

    final = _FinalResponse()
    fc_item = _Item("function_call", name="bash", args='{"command": "ls"}', call_id="c1")
    events = [
        _Evt("response.created"),
        _Evt("response.output_item.done", item=fc_item),
        _Evt("response.completed", response=final),
    ]
    result = _collect_codex_stream(iter(events))
    assert result is final
    assert result.output == [fc_item]


def test_collect_codex_stream_falls_back_to_iterator_attr():
    from minisweagent.models.oauth_response_model import _collect_codex_stream

    final = object()

    class _Iter:
        def __init__(self):
            self.completed_response = final
            self._items = iter([])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._items)

    assert _collect_codex_stream(_Iter()) is final


def test_codex_caller_kwargs_override_defaults(isolated_oauth, monkeypatch, register_stub_provider):
    """Caller-supplied kwargs (instructions, reasoning effort, etc.) must win
    over our pi-mono-derived defaults so users can tune the request."""
    import litellm

    from minisweagent.models.oauth_response_model import OAuthLitellmResponseModel

    class _Done:
        type = "response.completed"
        response = object()

    stub = _StubProvider(account_id="acc-1")
    register_stub_provider("openai-codex", stub)
    captured: dict = {}

    def fake_responses(*, model, input, tools, **kwargs):  # noqa: ARG001
        captured["messages"] = input
        captured.update(kwargs)
        return iter([_Done()])

    oauth.storage.save(
        "openai-codex",
        OAuthCredentials(refresh="r", access="x", expires=0, extra={"account_id": "acc-1"}),
    )
    model = OAuthLitellmResponseModel(
        model_name="openai/gpt-5", oauth_provider="openai-codex"
    )

    monkeypatch.setattr(litellm, "responses", fake_responses)
    model._query(
        [{"role": "system", "content": "from message"}, {"role": "user", "content": "hi"}],
        instructions="caller-set",
        reasoning={"effort": "high", "summary": "auto"},
    )

    assert captured["instructions"] == "caller-set"
    assert captured["reasoning"] == {"effort": "high", "summary": "auto"}


def test_anthropic_inject_claude_code_system(isolated_oauth, register_stub_provider):
    from minisweagent.models.oauth_model import CLAUDE_CODE_SYSTEM_PROMPT
    from minisweagent.models.oauth_response_model import OAuthLitellmResponseModel

    stub = _StubProvider()
    register_stub_provider("anthropic", stub)
    oauth.storage.save("anthropic", OAuthCredentials(refresh="r", access="x", expires=0))
    model = OAuthLitellmResponseModel(
        model_name="anthropic/claude-sonnet-4-5", oauth_provider="anthropic"
    )
    prepared = model._prepare_messages_for_api([{"role": "user", "content": "hi"}])
    assert prepared[0] == {"role": "system", "content": CLAUDE_CODE_SYSTEM_PROMPT}


def test_oauth_response_registered_in_model_class_mapping():
    from minisweagent.models import _MODEL_CLASS_MAPPING, get_model_class
    from minisweagent.models.oauth_response_model import OAuthLitellmResponseModel

    assert "oauth_response" in _MODEL_CLASS_MAPPING
    cls = get_model_class("openai/gpt-5.4", "oauth_response")
    assert cls is OAuthLitellmResponseModel
