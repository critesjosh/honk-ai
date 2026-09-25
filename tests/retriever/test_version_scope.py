"""Unit tests for the two-version retrieval-scoping layer.

Covers the pure logic in ``application/retriever/version_scope.py``:
the pre-retrieval version selector and the source-set narrowing resolver.
The DB-backed ``narrow_sources`` wrapper is exercised via an injected
fake repo so these stay pure (no Postgres).

The load-bearing invariant is the **no-op-until-cutover** property: with
today's unstamped sources (no ``metadata.version``) narrowing must keep
every source so live single-version behaviour is unchanged.
"""

from __future__ import annotations

from application.retriever.version_scope import (
    DEFAULT_VERSION,
    MAINNET_VERSION,
    TESTNET_VERSION,
    filter_sources_by_version,
    narrow_sources,
    select_active_version,
)


class TestSelectActiveVersion:
    def test_default_is_testnet_when_no_signal(self):
        assert select_active_version("how do private functions work?") == DEFAULT_VERSION
        assert DEFAULT_VERSION == TESTNET_VERSION

    def test_empty_and_none_question_default(self):
        assert select_active_version("") == DEFAULT_VERSION
        assert select_active_version(None) == DEFAULT_VERSION

    def test_explicit_mainnet_keyword(self):
        assert select_active_version("what is the mainnet rollup address?") == MAINNET_VERSION

    def test_explicit_testnet_keyword(self):
        assert select_active_version("how do I connect to testnet?") == TESTNET_VERSION

    def test_v4_version_token(self):
        assert select_active_version("does v4.3.1 support this?") == MAINNET_VERSION
        assert select_active_version("on v4 the API was different") == MAINNET_VERSION

    def test_v5_version_token(self):
        assert select_active_version("what changed in v5.0.0-rc.2?") == TESTNET_VERSION
        assert select_active_version("the v5 wallet API") == TESTNET_VERSION

    def test_ambiguous_both_named_falls_to_default(self):
        # Both networks named in the same question → ambiguous → default,
        # don't guess.
        q = "what's different between mainnet and testnet?"
        assert select_active_version(q) == DEFAULT_VERSION

    def test_question_signal_wins_over_history(self):
        history = [{"prompt": "tell me about mainnet"}]
        assert select_active_version("now on testnet, how do I deploy?", history) == (
            TESTNET_VERSION
        )

    def test_history_carries_forward_when_question_neutral(self):
        # Neutral follow-up inherits the established network from history.
        history = [{"prompt": "I'm running a mainnet node"}]
        assert select_active_version("how do I check sync status?", history) == (
            MAINNET_VERSION
        )

    def test_most_recent_history_signal_wins(self):
        history = [
            {"prompt": "earlier I asked about mainnet"},
            {"prompt": "actually let's talk about testnet"},
        ]
        assert select_active_version("and the faucet?", history) == TESTNET_VERSION

    def test_history_accepts_content_and_string_shapes(self):
        assert select_active_version("ok", [{"content": "on testnet"}]) == TESTNET_VERSION
        assert select_active_version("ok", ["talking about mainnet"]) == MAINNET_VERSION

    def test_sandbox_is_not_a_testnet_signal(self):
        # The local sandbox tracks whatever release the user installed, so it
        # must NOT pin testnet — falls through to the default.
        assert select_active_version("how do I start the sandbox?") == DEFAULT_VERSION

    def test_no_false_positive_on_embedded_digits(self):
        # "rev54" / "version 45" shouldn't trip the v5/v4 token matchers.
        assert select_active_version("see commit rev54 for the fix") == DEFAULT_VERSION
        assert select_active_version("there are 45 opcodes") == DEFAULT_VERSION


class TestFilterSourcesByVersion:
    def test_noop_when_no_version_metadata(self):
        # THE cutover-safety invariant: unstamped sources are all kept.
        ids = ["a", "b", "c"]
        version_map = {sid: {"version": None, "network": None} for sid in ids}
        assert filter_sources_by_version(ids, TESTNET_VERSION, version_map) == ids

    def test_noop_when_id_absent_from_map(self):
        # Missing map entry == unversioned == kept.
        ids = ["a", "b"]
        assert filter_sources_by_version(ids, TESTNET_VERSION, {}) == ids

    def test_narrows_to_active_version_plus_shared(self):
        version_map = {
            "main_docs": {"version": MAINNET_VERSION, "network": "mainnet"},
            "test_docs": {"version": TESTNET_VERSION, "network": "testnet"},
            "awesome": {"version": None, "network": "shared"},
        }
        ids = ["main_docs", "test_docs", "awesome"]
        assert filter_sources_by_version(ids, TESTNET_VERSION, version_map) == [
            "test_docs",
            "awesome",
        ]
        assert filter_sources_by_version(ids, MAINNET_VERSION, version_map) == [
            "main_docs",
            "awesome",
        ]

    def test_preserves_order(self):
        version_map = {
            "z": {"version": TESTNET_VERSION, "network": "testnet"},
            "a": {"version": TESTNET_VERSION, "network": "testnet"},
            "m": {"version": MAINNET_VERSION, "network": "mainnet"},
        }
        # Canonical retrieval order must survive the filter.
        assert filter_sources_by_version(["z", "m", "a"], TESTNET_VERSION, version_map) == (
            ["z", "a"]
        )

    def test_shared_kept_for_either_version(self):
        version_map = {"awesome": {"version": None, "network": "shared"}}
        assert filter_sources_by_version(["awesome"], MAINNET_VERSION, version_map) == (
            ["awesome"]
        )
        assert filter_sources_by_version(["awesome"], TESTNET_VERSION, version_map) == (
            ["awesome"]
        )

    def test_explicit_shared_version_string_kept(self):
        # A source tagged shared but ALSO carrying a version is still kept for
        # any active version (network==shared short-circuits the version check).
        version_map = {"x": {"version": MAINNET_VERSION, "network": "shared"}}
        assert filter_sources_by_version(["x"], TESTNET_VERSION, version_map) == ["x"]

    def test_empty_input(self):
        assert filter_sources_by_version([], TESTNET_VERSION, {}) == []

    def test_fallback_to_full_set_when_narrowing_empties(self):
        # Misconfigured set: only the OTHER version, no shared, no unversioned.
        # Retrieving over nothing is worse than retrieving the wrong version,
        # so we fall back to the full input (and log).
        version_map = {"m": {"version": MAINNET_VERSION, "network": "mainnet"}}
        assert filter_sources_by_version(["m"], TESTNET_VERSION, version_map) == ["m"]


class _FakeRepo:
    def __init__(self, version_map):
        self._map = version_map

    def version_metadata_for_ids(self, ids):
        return {sid: self._map[sid] for sid in ids if sid in self._map}


class TestNarrowSources:
    def test_wrapper_applies_filter_via_injected_repo(self):
        version_map = {
            "m": {"version": MAINNET_VERSION, "network": "mainnet"},
            "t": {"version": TESTNET_VERSION, "network": "testnet"},
        }
        out = narrow_sources(
            ["m", "t"], TESTNET_VERSION, repo_factory=lambda: _FakeRepo(version_map)
        )
        assert out == ["t"]

    def test_wrapper_drops_blank_ids(self):
        out = narrow_sources(
            ["", "  ", "t"],
            TESTNET_VERSION,
            repo_factory=lambda: _FakeRepo(
                {"t": {"version": TESTNET_VERSION, "network": "testnet"}}
            ),
        )
        assert out == ["t"]

    def test_wrapper_degrades_to_full_set_on_repo_error(self):
        def boom():
            raise RuntimeError("db down")

        # A DB failure must never take the request down — return the input.
        assert narrow_sources(["a", "b"], TESTNET_VERSION, repo_factory=boom) == ["a", "b"]

    def test_wrapper_empty_input(self):
        assert narrow_sources([], TESTNET_VERSION, repo_factory=lambda: _FakeRepo({})) == []
