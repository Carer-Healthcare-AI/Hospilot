"""OpenAI text-embedding client for cross-session memory retrieval.

Anthropic has no embeddings API, so vectors for semantic fact retrieval come
from OpenAI (text-embedding-3-small, 1536-dim by default). Used by rag.memory:
facts are embedded on write, the question on read, and cosine is computed
app-side. Every call degrades gracefully -- a missing key or transport error
returns None(s) so retrieval falls back to recency order rather than failing
/api/ask.

    from db import embeddings
    vec  = await embeddings.embed_text("...")        # list[float] | None
    vecs = await embeddings.embed_texts(["a", "b"])  # list[list[float] | None]
"""

import logging

import httpx
from config import settings

logger = logging.getLogger("db.embeddings")

_TIMEOUT = 20.0


def _enabled() -> bool:
    return bool(settings.openai_api_key)


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embed a batch. Returns one vector per input (aligned by index), or a list
    of None the same length on failure / when disabled. Empty strings -> None."""
    if not texts:
        return []
    if not _enabled():
        return [None] * len(texts)

    # OpenAI rejects empty strings; embed only the non-empty ones and re-align.
    idx = [i for i, t in enumerate(texts) if (t or "").strip()]
    if not idx:
        return [None] * len(texts)
    payload = {"model": settings.embedding_model, "input": [texts[i] for i in idx]}
    url = settings.openai_base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}",
               "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json().get("data", [])
        out: list[list[float] | None] = [None] * len(texts)
        # Response order matches input order, but honour the returned `index`.
        for item in data:
            pos = item.get("index")
            emb = item.get("embedding")
            if pos is not None and 0 <= pos < len(idx) and emb:
                out[idx[pos]] = emb
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed_texts failed (%d texts): %s", len(idx), exc)
        return [None] * len(texts)


async def embed_text(text: str) -> list[float] | None:
    return (await embed_texts([text]))[0]
