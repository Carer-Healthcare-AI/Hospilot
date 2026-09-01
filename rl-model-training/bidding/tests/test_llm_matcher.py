"""The LLM fallback changes nothing unless it is switched on — and then only safely.

The API call itself is faked everywhere: these tests pin the *contract* around the call —
opt-in gating, memoization, "unknown" raising, and the trace saying "llm" rather than
pretending tokens matched. The one thing deliberately not tested is Claude's judgement.
"""

from __future__ import annotations

import pytest

from allocation.contracts import ResourceType
from allocation.trigger import llm_matcher
from allocation.trigger.query import UnknownUseCase, matched_tokens, resolve_profile

# The token matcher must find nothing in this, or the fallback is never consulted.
FREE_FORM = "at 3pm a spot will open up in intensive care"


@pytest.fixture(autouse=True)
def clean_memo():
    llm_matcher._memo.clear()
    yield
    llm_matcher._memo.clear()


def test_disabled_by_default_and_never_calls_out(monkeypatch):
    """Without the env var the module is inert — no call, same error as before it existed."""
    monkeypatch.delenv("ALLOCATION_LLM_QUERY", raising=False)
    monkeypatch.setattr(
        llm_matcher, "_ask_claude", lambda query: pytest.fail("called while disabled")
    )
    assert llm_matcher.resolve(FREE_FORM) is None
    with pytest.raises(UnknownUseCase, match="ALLOCATION_LLM_QUERY"):
        resolve_profile(FREE_FORM)


def test_enabled_resolves_a_free_form_query(monkeypatch):
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    monkeypatch.setattr(llm_matcher, "_ask_claude", lambda query: ResourceType.ICU_BED)
    assert resolve_profile(FREE_FORM).resource_type is ResourceType.ICU_BED


def test_unknown_still_raises_with_no_default(monkeypatch):
    """The LLM saying "unknown" must land on the exact error path the matcher uses."""
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    monkeypatch.setattr(llm_matcher, "_ask_claude", lambda query: None)
    # Not "HDU bed" any more — that resolves to hdu_bed on tokens alone now that the bed
    # family is registered, so it would never reach the fallback this test is about.
    with pytest.raises(UnknownUseCase, match="no registered resource"):
        resolve_profile("allocate a ventilator")


def test_token_matcher_stays_first(monkeypatch):
    """A query the tokens resolve must never reach the LLM, even when enabled."""
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    monkeypatch.setattr(
        llm_matcher, "_ask_claude", lambda query: pytest.fail("tokens matched; llm consulted")
    )
    assert resolve_profile("one limited ICU bed").resource_type is ResourceType.ICU_BED


def test_resolutions_are_memoized(monkeypatch):
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    calls: list[str] = []

    def fake(query: str) -> ResourceType:
        calls.append(query)
        return ResourceType.ICU_BED

    monkeypatch.setattr(llm_matcher, "_ask_claude", fake)
    resolve_profile(FREE_FORM)
    resolve_profile(FREE_FORM)
    assert len(calls) == 1


def test_the_trace_says_llm_not_tokens(monkeypatch):
    """A semantic resolution must not masquerade as a token match or a supplied profile."""
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    monkeypatch.setattr(llm_matcher, "_ask_claude", lambda query: ResourceType.ICU_BED)
    profile = resolve_profile(FREE_FORM)
    assert "llm" in matched_tokens(FREE_FORM, profile)
    # A token-matched query still reports its tokens, not the llm.
    assert matched_tokens("one limited ICU bed", profile) == "icu + bed"


def test_sdk_missing_degrades_to_the_matcher_error(monkeypatch):
    """anthropic isn't installed in the core environment; enabled-but-unavailable raises."""
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    with pytest.raises(UnknownUseCase):
        resolve_profile(FREE_FORM)


# -- the request itself, which nothing else exercises --------------------------------------


def _capture_request(monkeypatch):
    """Run the real ``_ask_claude`` against a stub SDK and return the kwargs it sent.

    ``anthropic`` is not installed in the core environment and there is no API key, so
    ``_ask_claude`` is the one function in this module that no other test reaches — it
    returns None at the ImportError before touching a single parameter. A stub module is
    the only way to pin the request shape.
    """
    import sys
    import types

    sent: dict = {}

    class _Block:
        type = "text"
        text = '{"resource_type": "icu_bed"}'

    class _Response:
        stop_reason = "end_turn"
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return _Response()

    class _Anthropic:
        def __init__(self, **kwargs):
            self.beta = types.SimpleNamespace(messages=_Messages())

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_Anthropic))
    assert llm_matcher._ask_claude("one limited ICU bed") is ResourceType.ICU_BED
    return sent


def test_thinking_is_disabled_so_max_tokens_is_not_shared_with_reasoning(monkeypatch):
    """Thinking is on by default on Claude Opus 5, and max_tokens caps thinking + text.

    A long think would return stop_reason "max_tokens", `_ask_claude` would return None, and
    the query would degrade to UnknownUseCase — indistinguishable from a genuine "unknown".
    """
    sent = _capture_request(monkeypatch)
    assert sent["thinking"] == {"type": "disabled"}


def test_disabled_thinking_and_effort_stay_compatible(monkeypatch):
    """The API rejects thinking: disabled at effort `xhigh` or `max`.

    Raising effort without re-enabling thinking is a 400 that no other test would catch,
    because this request is never made in the test environment.
    """
    sent = _capture_request(monkeypatch)
    if sent.get("thinking", {}).get("type") == "disabled":
        assert sent["output_config"]["effort"] in {"low", "medium", "high"}


def test_the_fallback_beta_matches_the_fallbacks_form(monkeypatch):
    """`fallbacks: "default"` pairs with -2026-07-01; the array form pairs with -2026-06-01.

    Crossing them is a 400.
    """
    sent = _capture_request(monkeypatch)
    if sent.get("fallbacks") == "default":
        assert "server-side-fallback-2026-07-01" in sent["betas"]
    elif isinstance(sent.get("fallbacks"), list):
        assert "server-side-fallback-2026-06-01" in sent["betas"]


def test_the_answer_is_constrained_to_registered_resources(monkeypatch):
    """The model cannot invent a profile that does not exist."""
    sent = _capture_request(monkeypatch)
    schema = sent["output_config"]["format"]["schema"]
    allowed = set(schema["properties"]["resource_type"]["enum"])
    assert allowed == {r.value for r in ResourceType} | {"unknown"}


# -- the prompt cannot contradict the registry it is built from ---------------------------


def test_the_prompt_lists_every_registered_resource():
    prompt = llm_matcher._system_prompt()
    for resource in ResourceType:
        assert f'"{resource.value}"' in prompt, f"{resource.value} missing from the prompt"


def test_the_prompt_does_not_forbid_a_resource_it_offers():
    """F-B: the prompt used to say HDU and PACU beds "are unknown" while listing them.

    The resource list is generated from the registry; that sentence was literal text. Once
    hdu_bed and pacu_bed registered, the prompt named them and forbade them at once. Any rule
    naming a specific unit is at risk of the same drift, so the guard is on the shape of the
    prompt rather than on that one sentence.
    """
    prompt = llm_matcher._system_prompt().lower()
    for resource in ResourceType:
        unit = resource.unit
        assert f'{unit} beds are alternative' not in prompt
        assert f'not {unit} beds' not in prompt
    assert "alternative pathways, not" not in prompt


# -- an ambiguous query: tokens narrow, the model chooses ---------------------------------

AMBIGUOUS = "we have an ICU bed and a ward bed free"


def test_a_tie_raises_when_the_fallback_is_disabled(monkeypatch):
    """The default state, and the state every other test runs in."""
    monkeypatch.delenv("ALLOCATION_LLM_QUERY", raising=False)
    with pytest.raises(UnknownUseCase, match="more than one resource"):
        resolve_profile(AMBIGUOUS)


def test_the_fallback_breaks_a_tie(monkeypatch):
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    monkeypatch.setattr(llm_matcher, "_ask_claude", lambda query: ResourceType.WARD_BED)
    assert resolve_profile(AMBIGUOUS).resource_type is ResourceType.WARD_BED


def test_the_fallback_may_only_choose_from_what_the_tokens_found(monkeypatch):
    """Tokens establish which readings are plausible; the model picks between them.

    An answer outside that set is not a disambiguation, it is an override — and overriding
    two explicitly named units with a third would auction a bed nobody mentioned.
    """
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    monkeypatch.setattr(llm_matcher, "_ask_claude", lambda query: ResourceType.PACU_BED)
    with pytest.raises(UnknownUseCase, match="more than one resource"):
        resolve_profile(AMBIGUOUS)


def test_a_tie_broken_by_the_llm_says_so_in_the_trace(monkeypatch):
    """Both profiles' tokens matched, so reporting "ward + bed" would hide the ambiguity."""
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    monkeypatch.setattr(llm_matcher, "_ask_claude", lambda query: ResourceType.WARD_BED)
    profile = resolve_profile(AMBIGUOUS)
    trace = matched_tokens(AMBIGUOUS, profile)
    assert "llm chose this one" in trace
    assert "icu_bed" in trace and "ward_bed" in trace


def test_proximity_settles_it_without_ever_consulting_the_model(monkeypatch):
    """The founding query names two units. It must not need an LLM to resolve."""
    monkeypatch.setenv("ALLOCATION_LLM_QUERY", "1")
    monkeypatch.setattr(
        llm_matcher, "_ask_claude", lambda query: pytest.fail("proximity settled it; llm called")
    )
    query = "ER, OT, and ICU/Ward demand compete for one limited ICU bed"
    assert resolve_profile(query).resource_type is ResourceType.ICU_BED
