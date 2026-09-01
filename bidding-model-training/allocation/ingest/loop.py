"""One event loop for the whole process. A real bug, not a tidiness preference.

``DataSource`` is async because the production reader will talk to Hasura over the network. Every
synchronous caller therefore has to bridge into async, and the obvious bridge is ``asyncio.run``
— which **creates and destroys an entire event loop per call**.

That is fine once. It is not fine at training scale. One CEM generation runs ~24 simulations,
each with ~70 auctions, each with 3 rounds, each reading a snapshot plus up to two alternative
units: on the order of 10⁴ event loops per generation. On Windows every loop constructs a
self-pipe via ``socket.socketpair()``, and ``socket.py`` has no native socketpair there — it
falls back to binding a real localhost TCP socket. Do that tens of thousands of times and the
sockets pile up in ``TIME_WAIT`` until the next bind blocks.

**That is exactly what happened.** Training completed three or four generations and then hung
with memory frozen and no CPU. A ``faulthandler`` watchdog caught it mid-hang::

    Timeout (0:01:30)!
    Thread 0x00005f3c (most recent call first):
      File "C:\\Python314\\Lib\\socket.py", line 629 in _fallback_socketpair

Not in any allocation module — blocked inside loop construction. It looked like slowness for a
long time, which is why it went undiagnosed through several runs.

The fix is one loop, created once, reused. ``run`` is a drop-in for ``asyncio.run`` for this
codebase's purposes, and it deliberately does *not* close the loop: closing and recreating is the
behaviour that caused the problem.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")

_local = threading.local()


def _loop() -> asyncio.AbstractEventLoop:
    """This thread's event loop, created once and kept.

    Thread-local rather than a module global because an event loop is not safe to share across
    threads, and a test runner or a future worker pool may well use several.
    """
    loop = getattr(_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _local.loop = loop
    return loop


def run(coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion on the shared loop.

    Falls back to ``asyncio.run`` when a loop is already running in this thread — that only
    happens if an async caller reaches a sync bridge, which would deadlock on the shared loop.
    Rare, and better to be slow than to hang.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _loop().run_until_complete(coro)
    return asyncio.run(coro)
