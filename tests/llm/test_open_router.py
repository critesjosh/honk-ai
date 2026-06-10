"""Unit tests for the OpenRouter reasoning policy.

Covers ``_apply_reasoning_policy`` precedence: per-agent opt-in (enable with
a max_tokens budget, gated on the call offering tools) wins over the
model-prefix disable, which wins over the provider default (no-op).
"""

from unittest.mock import patch

from application.llm import open_router


def _policy(agent_id, model, *, enabled_ids="", max_tokens=16000, tools=None):
    """Run the policy with patched settings; return the mutated kwargs."""
    kwargs = {}
    with patch.object(open_router.settings, "REASONING_ENABLED_AGENT_IDS", enabled_ids), \
         patch.object(open_router.settings, "REASONING_MAX_TOKENS", max_tokens):
        open_router._apply_reasoning_policy(agent_id, model, kwargs, tools=tools)
    return kwargs


DISABLED_MODEL = "qwen/qwen3.6-flash"
OTHER_MODEL = "anthropic/claude-sonnet-4.6"
SLACK_AGENT = "f7ae0b19-5f65-452c-8ca9-24a73f471252"
TOOLS = [{"type": "function", "function": {"name": "aztec_network_search"}}]


class TestApplyReasoningPolicy:
    def test_opted_in_agent_with_tools_enables_with_budget(self):
        kwargs = _policy(SLACK_AGENT, DISABLED_MODEL, enabled_ids=SLACK_AGENT, max_tokens=16000, tools=TOOLS)
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 16000}

    def test_opt_in_overrides_model_disable(self):
        # Same model is in _REASONING_DISABLED_MODEL_PREFIXES, but the agent
        # opt-in must win — enabled-with-budget, NOT {enabled: false}.
        kwargs = _policy(SLACK_AGENT, DISABLED_MODEL, enabled_ids=SLACK_AGENT, tools=TOOLS)
        assert "enabled" not in kwargs["extra_body"]["reasoning"]
        assert "max_tokens" in kwargs["extra_body"]["reasoning"]

    def test_opted_in_agent_without_tools_keeps_model_disable(self):
        # The opt-in exists for tool-call routing; no-tool utility calls
        # (rephrase, compression) under the same agent must NOT burn the
        # reasoning budget — flagged model falls back to {enabled: false}.
        kwargs = _policy(SLACK_AGENT, DISABLED_MODEL, enabled_ids=SLACK_AGENT, tools=None)
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_opted_in_agent_with_empty_tools_keeps_model_disable(self):
        # An empty tools list is "no tools offered", same as None.
        kwargs = _policy(SLACK_AGENT, DISABLED_MODEL, enabled_ids=SLACK_AGENT, tools=[])
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_opted_in_agent_without_tools_non_flagged_model_is_noop(self):
        kwargs = _policy(SLACK_AGENT, OTHER_MODEL, enabled_ids=SLACK_AGENT, tools=None)
        assert "extra_body" not in kwargs

    def test_disabled_model_without_optin_disables(self):
        kwargs = _policy("some-other-agent", DISABLED_MODEL, enabled_ids=SLACK_AGENT, tools=TOOLS)
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_non_disabled_model_without_optin_is_noop(self):
        kwargs = _policy("some-other-agent", OTHER_MODEL, enabled_ids="", tools=TOOLS)
        assert "extra_body" not in kwargs

    def test_none_agent_falls_through_to_model_policy(self):
        # No agent id (e.g. non-agent call) → not opted in → model disable applies.
        kwargs = _policy(None, DISABLED_MODEL, enabled_ids=SLACK_AGENT, tools=TOOLS)
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_csv_allowlist_with_whitespace(self):
        kwargs = _policy(SLACK_AGENT, DISABLED_MODEL, enabled_ids=f"  other , {SLACK_AGENT} ", tools=TOOLS)
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 16000}

    def test_empty_allowlist_disables_flagged_model(self):
        kwargs = _policy(SLACK_AGENT, DISABLED_MODEL, enabled_ids="", tools=TOOLS)
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_budget_value_is_configurable(self):
        kwargs = _policy(SLACK_AGENT, DISABLED_MODEL, enabled_ids=SLACK_AGENT, max_tokens=8000, tools=TOOLS)
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 8000}

    def test_preserves_existing_extra_body(self):
        kwargs = {"extra_body": {"foo": "bar"}}
        with patch.object(open_router.settings, "REASONING_ENABLED_AGENT_IDS", SLACK_AGENT), \
             patch.object(open_router.settings, "REASONING_MAX_TOKENS", 16000):
            open_router._apply_reasoning_policy(SLACK_AGENT, DISABLED_MODEL, kwargs, tools=TOOLS)
        assert kwargs["extra_body"]["foo"] == "bar"
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 16000}

    def test_authoritative_overwrites_preexisting_reasoning(self):
        # The policy is the single source of the reasoning field — a
        # pre-existing value must be overwritten, not preserved.
        kwargs = {"extra_body": {"reasoning": {"enabled": False}}}
        with patch.object(open_router.settings, "REASONING_ENABLED_AGENT_IDS", SLACK_AGENT), \
             patch.object(open_router.settings, "REASONING_MAX_TOKENS", 16000):
            open_router._apply_reasoning_policy(SLACK_AGENT, DISABLED_MODEL, kwargs, tools=TOOLS)
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 16000}


class TestRawGenStreamWiring:
    """Confirm the policy is actually wired into the transport: the LLM's
    own ``agent_id`` drives it, the tools gate sees the call's ``tools``,
    and both ``extra_body`` and ``tools`` reach super()."""

    def _capture(self, agent_id, enabled_ids, tools=None):
        from application.llm.openai import OpenAILLM

        llm = open_router.OpenRouterLLM()
        llm.agent_id = agent_id
        captured = {}

        def fake_super(self_, baseself, model, messages, stream=True, tools=None, *a, **kw):
            captured.update(kw)
            captured["tools"] = tools
            return iter(())

        with patch.object(OpenAILLM, "_raw_gen_stream", fake_super), \
             patch.object(open_router.settings, "REASONING_ENABLED_AGENT_IDS", enabled_ids), \
             patch.object(open_router.settings, "REASONING_MAX_TOKENS", 16000):
            list(llm._raw_gen_stream(llm, DISABLED_MODEL, [], tools=tools))
        return captured

    def test_opted_in_agent_with_tools_gets_reasoning_through_transport(self):
        captured = self._capture(SLACK_AGENT, SLACK_AGENT, tools=TOOLS)
        assert captured["extra_body"]["reasoning"] == {"max_tokens": 16000}
        assert captured["tools"] == TOOLS

    def test_opted_in_agent_without_tools_stays_disabled_through_transport(self):
        # e.g. ClassicRAG._rephrase_query / compression: same agent_id, no
        # tools — must NOT get the reasoning budget.
        captured = self._capture(SLACK_AGENT, SLACK_AGENT, tools=None)
        assert captured["extra_body"]["reasoning"] == {"enabled": False}
        assert captured["tools"] is None

    def test_other_agent_keeps_reasoning_disabled_through_transport(self):
        captured = self._capture("discord-agent-id", SLACK_AGENT, tools=TOOLS)
        assert captured["extra_body"]["reasoning"] == {"enabled": False}

    def test_raw_gen_nonstreaming_gates_on_tools_too(self):
        # Same wiring for the non-streaming path: opted-in + tools enables,
        # opted-in without tools falls back to the model disable.
        from application.llm.openai import OpenAILLM

        llm = open_router.OpenRouterLLM()
        llm.agent_id = SLACK_AGENT
        captured = {}

        def fake_super(self_, baseself, model, messages, stream=False, tools=None, *a, **kw):
            captured.update(kw)
            return "ok"

        with patch.object(OpenAILLM, "_raw_gen", fake_super), \
             patch.object(open_router.settings, "REASONING_ENABLED_AGENT_IDS", SLACK_AGENT), \
             patch.object(open_router.settings, "REASONING_MAX_TOKENS", 16000):
            llm._raw_gen(llm, DISABLED_MODEL, [], tools=TOOLS)
            assert captured["extra_body"]["reasoning"] == {"max_tokens": 16000}
            captured.clear()
            llm._raw_gen(llm, DISABLED_MODEL, [], tools=None)
            assert captured["extra_body"]["reasoning"] == {"enabled": False}

    def test_tools_passed_positionally_still_gate_the_policy(self):
        # The usage decorators call _raw_gen_stream positionally:
        # func(self, model, messages, stream, tools, **kwargs). The named
        # capture in the override must bind that 4th positional arg.
        from application.llm.openai import OpenAILLM

        llm = open_router.OpenRouterLLM()
        llm.agent_id = SLACK_AGENT
        captured = {}

        def fake_super(self_, baseself, model, messages, stream=True, tools=None, *a, **kw):
            captured.update(kw)
            return iter(())

        with patch.object(OpenAILLM, "_raw_gen_stream", fake_super), \
             patch.object(open_router.settings, "REASONING_ENABLED_AGENT_IDS", SLACK_AGENT), \
             patch.object(open_router.settings, "REASONING_MAX_TOKENS", 16000):
            list(llm._raw_gen_stream(llm, DISABLED_MODEL, [], True, TOOLS))
        assert captured["extra_body"]["reasoning"] == {"max_tokens": 16000}
