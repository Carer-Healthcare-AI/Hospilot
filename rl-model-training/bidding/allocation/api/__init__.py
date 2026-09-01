"""HTTP surface over the allocation engine.

``allocation.api`` is a **wrapper, not a layer**. Every number it returns comes from
:func:`~allocation.trigger.runtime.run_allocation` and
:func:`~allocation.trigger.session.run_session` — the same functions the CLI calls, in the
same order. If a formula ever appears under ``api/``, a layer has been bypassed and the HTTP
answer has stopped matching the CLI answer for the same inputs.

Run it::

    pip install -e ".[api]"
    python -m allocation.api                    # http://127.0.0.1:8000
    curl -s localhost:8000/health
    curl -s -X POST localhost:8000/auction -H 'content-type: application/json' -d '{}'

Interactive docs are at ``/docs``; the OpenAPI schema at ``/openapi.json``.

**Three things this package deliberately refuses**, all for the same reason — an HTTP endpoint
is reachable by anything on the network, and the CLI's escape hatches assume a person at a
keyboard who knows what the fixture data is:

* ``mode: live`` — refused unconditionally, as in ``cli.py``. The shipped data source serves
  three invented patients from Appendix C; holding a real bed for them would be the worst
  failure this system has. The CLI refuses it too, and the API must not be the softer door.
* **Filesystem paths in a request body.** ``--config-dir`` and ``--scenario`` take arbitrary
  paths on the command line. Over HTTP that is an arbitrary-file-read primitive, so the
  config directory is fixed per *process* (``--config-dir`` at startup) and scenarios are
  addressed by **name** out of a fixed directory (``GET /scenarios``), never by path.
* **Persistence.** :class:`~allocation.api.service.RunStore` keeps the last N runs in memory so
  ``GET /auction/{id}`` works after the fact. That is a convenience for curl, not a log —
  the real one is migration 091, and until it runs nothing here survives a restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from allocation.api.app import create_app


def __getattr__(name: str):
    """Import FastAPI lazily so ``import allocation.api`` costs nothing without the extra."""
    if name == "create_app":
        try:
            from allocation.api.app import create_app
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
            raise ModuleNotFoundError(
                "the HTTP API needs FastAPI and uvicorn, which are an optional extra: "
                'pip install -e ".[api]"'
            ) from exc
        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["create_app"]
