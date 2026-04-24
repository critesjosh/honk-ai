import sys
import types

import pytest


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeMessagesResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeStreamContext:
    def __init__(self, texts):
        self.text_stream = iter(texts)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeMessages:
    def __init__(self):
        self.last_create_kwargs = None
        self.last_stream_kwargs = None
        self._stream_texts = ["s1", "s2"]

    def create(self, **kwargs):
        self.last_create_kwargs = kwargs
        return _FakeMessagesResponse("final")

    def stream(self, **kwargs):
        self.last_stream_kwargs = kwargs
        return _FakeStreamContext(self._stream_texts)


class _FakeAnthropic:
    def __init__(self, api_key=None, base_url=None):
        self.api_key = api_key
        self.base_url = base_url
        self.messages = _FakeMessages()


@pytest.fixture(autouse=True)
def patch_anthropic(monkeypatch):
    fake = types.ModuleType("anthropic")
    fake.Anthropic = _FakeAnthropic

    modules_to_remove = [key for key in sys.modules if key.startswith("anthropic")]
    for key in modules_to_remove:
        sys.modules.pop(key, None)
    sys.modules["anthropic"] = fake

    if "application.llm.anthropic" in sys.modules:
        del sys.modules["application.llm.anthropic"]
    yield

    sys.modules.pop("anthropic", None)
    if "application.llm.anthropic" in sys.modules:
        del sys.modules["application.llm.anthropic"]


def test_anthropic_raw_gen_builds_prompt_and_returns_completion():
    from application.llm.anthropic import AnthropicLLM

    llm = AnthropicLLM(api_key="k")
    msgs = [
        {"content": "ctx"},
        {"content": "q"},
    ]
    out = llm._raw_gen(
        llm, model="claude-2", messages=msgs, stream=False, max_tokens=55
    )
    assert out == "final"
    last = llm.anthropic.messages.last_create_kwargs
    assert last["model"] == "claude-2"
    assert last["max_tokens"] == 55
    convo = last["messages"]
    assert all(m["role"] in ("user", "assistant") for m in convo)
    combined = " ".join(m["content"] for m in convo)
    assert "### Context" in combined and "### Question" in combined


def test_anthropic_raw_gen_stream_yields_chunks():
    from application.llm.anthropic import AnthropicLLM

    llm = AnthropicLLM(api_key="k")
    msgs = [
        {"content": "ctx"},
        {"content": "q"},
    ]
    gen = llm._raw_gen_stream(
        llm, model="claude", messages=msgs, stream=True, max_tokens=10
    )
    chunks = list(gen)
    assert chunks == ["s1", "s2"]
