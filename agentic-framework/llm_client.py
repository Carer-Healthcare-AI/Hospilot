"""Unified LLM client -- the single chokepoint for every chat-completion call
in the app.

Switch between Claude and any OpenAI-compatible server (Ollama, vLLM ...) by
setting LLM_PROVIDER in the .env file:

  LLM_PROVIDER=anthropic  -> uses ANTHROPIC_API_KEY + LLM_FAST_MODEL/LLM_QUALITY_MODEL
                             (Claude model names; sensible defaults if unset)
  LLM_PROVIDER=openai     -> uses LLM_BASE_URL + LLM_FAST_MODEL/LLM_QUALITY_MODEL
                             (Ollama / vLLM model tags)

Two tiers stand in for Claude's haiku/sonnet split: "fast" for cheap
classification/guardrail calls, "quality" for planning/synthesis/codegen.
Callers never instantiate a provider client themselves -- use llm_chat() for
plain text, llm_json_prefill() when the reply must be a bare JSON object, and
llm_agentic_loop() for multi-round tool-calling.
"""

import json
import logging
import re

from config import settings

logger = logging.getLogger("llm_client")

# Qwen3 (and similar models) emit <think>...</think> blocks before the actual
# answer. Strip them so callers only receive the final response text.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

_DEFAULT_MODEL = {
    "anthropic": {"fast": "claude-haiku-4-5-20251001", "quality": "claude-sonnet-4-6"},
}


def strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _model_for(tier: str) -> str:
    configured = settings.llm_fast_model if tier == "fast" else settings.llm_quality_model
    if configured:
        return configured
    if settings.llm_provider == "openai":
        return settings.llm_model
    return _DEFAULT_MODEL["anthropic"][tier]


def get_llm_client():
    """Return (client, provider) for whichever backend is configured."""
    if settings.llm_provider == "openai":
        import openai
        client = openai.AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key="none",
            timeout=600.0,  # 10 min -- critic calls on large plans can take several minutes
        )
        return client, "openai"

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return client, "anthropic"


async def llm_chat(system: str = "", user: str = "", max_tokens: int = 1024,
                   temperature: float = 0, tier: str = "quality") -> str:
    """Call whichever LLM is configured and return the response as plain text."""
    client, provider = get_llm_client()
    model = _model_for(tier)

    if provider == "openai":
        # For small requests (guardrail, conditions) skip Qwen3 thinking mode --
        # it wastes time on simple yes/no answers. For large requests keep thinking.
        if max_tokens <= 200:
            effective_max = 512
            user = "/no_think\n" + user
        else:
            effective_max = max_tokens
        response = await client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=effective_max,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            extra_body={"options": {"num_ctx": 16384}},
        )
        return strip_thinking(response.choices[0].message.content)

    kwargs = dict(model=model, max_tokens=max_tokens, temperature=temperature,
                  messages=[{"role": "user", "content": user}])
    if system:
        kwargs["system"] = system
    response = await client.messages.create(**kwargs)
    return response.content[0].text.strip()


async def llm_json_prefill(user: str, max_tokens: int = 1024, temperature: float = 0,
                           tier: str = "quality") -> str:
    """Ask for a JSON object; returns raw text starting with '{'.

    Anthropic gets an assistant-message prefill (forces JSON, no prose) --
    OpenAI-compatible backends don't support prefill the same way, so they get
    an explicit instruction instead. Callers still need their own markdown-
    fence stripping for the openai path (models sometimes wrap JSON in ```
    fences despite the instruction)."""
    client, provider = get_llm_client()
    model = _model_for(tier)

    if provider == "openai":
        return await llm_chat(
            system="Return ONLY a raw JSON object. No prose, no markdown fences.",
            user=user, max_tokens=max_tokens, temperature=temperature, tier=tier,
        )

    response = await client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        messages=[
            {"role": "user", "content": user},
            {"role": "assistant", "content": "{"},
        ],
    )
    return "{" + response.content[0].text.strip()


def _to_openai_tools(tool_schemas: list[dict]) -> list[dict]:
    """Anthropic-shaped {name, description, input_schema} -> OpenAI's
    {type: function, function: {name, description, parameters}}."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tool_schemas
    ]


async def llm_agentic_loop(user: str, tool_schemas: list[dict], tool_impls: dict,
                           max_tokens: int = 2048, max_rounds: int = 4,
                           tier: str = "quality", context_label: str = "") -> str:
    """Multi-round tool-use loop, provider-agnostic.

    tool_schemas are Anthropic-shaped ({name, description, input_schema}),
    translated to OpenAI's tool-call shape when the configured provider is
    openai/Ollama. tool_impls maps tool name -> async callable (no arguments;
    these are all data-fetch tools with empty input schemas)."""
    client, provider = get_llm_client()
    model = _model_for(tier)

    async def _call_tool(name: str) -> str:
        fn = tool_impls.get(name)
        try:
            data = await fn() if fn else None
            if data is None:
                return json.dumps({"error": f"unknown tool: {name}"})
            return json.dumps(data, default=str)[:8000]
        except Exception as exc:
            logger.warning("fetch tool error  context=%s  tool=%s  err=%s",
                            context_label, name, exc)
            return json.dumps({"error": str(exc)})

    if provider == "openai":
        messages = [{"role": "user", "content": user}]
        tools = _to_openai_tools(tool_schemas)
        for round_num in range(max_rounds):
            resp = await client.chat.completions.create(
                model=model, max_tokens=max_tokens, tools=tools, messages=messages,
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return strip_thinking(msg.content or "")
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                content = await _call_tool(tc.function.name)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
            logger.debug("tool-use round %d  context=%s  tools_called=%s",
                         round_num + 1, context_label, [tc.function.name for tc in msg.tool_calls])
        messages.append({"role": "user", "content": "Now return your final JSON answer only."})
        final = await client.chat.completions.create(
            model=model, max_tokens=max_tokens, messages=messages,
        )
        return strip_thinking(final.choices[0].message.content or "")

    # anthropic
    messages = [{"role": "user", "content": user}]
    for round_num in range(max_rounds):
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, tools=tool_schemas, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            parts = [b.text for b in resp.content if hasattr(b, "text")]
            return "\n".join(parts).strip()
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            content = await _call_tool(block.name)
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
        messages = messages + [
            {"role": "assistant", "content": resp.content},
            {"role": "user",      "content": tool_results},
        ]
        logger.debug("tool-use round %d  context=%s  tools_called=%s",
                     round_num + 1, context_label, [b.name for b in resp.content if b.type == "tool_use"])

    messages.append({"role": "user", "content": "Now return your final JSON answer only."})
    final = await client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
    parts = [b.text for b in final.content if hasattr(b, "text")]
    return "\n".join(parts).strip()
