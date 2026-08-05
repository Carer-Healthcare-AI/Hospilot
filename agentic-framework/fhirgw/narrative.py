"""Generated text narratives (FHIR dom-6 best practice).

Every DomainResource "should have narrative for robust management". We emit a
minimal machine-generated XHTML summary built purely from the structured data,
so `status` is always 'generated' (no human-authored text mixed in).

Narrative is write-only: the to_internal mappers never read `.text`, so adding
it does not affect round-trip identity.
"""

import html

XHTML_NS = "http://www.w3.org/1999/xhtml"


def text(summary: str) -> dict:
    """A FHIR Narrative wrapping `summary` as escaped XHTML."""
    safe = html.escape(summary) if summary else "--"
    return {"status": "generated", "div": f'<div xmlns="{XHTML_NS}">{safe}</div>'}
