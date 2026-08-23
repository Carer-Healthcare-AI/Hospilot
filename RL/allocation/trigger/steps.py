"""A structured trace of what the run did, stage by stage.

The point is **verifiability against the framework**. Each :class:`Step` carries the RL-Steps
section it implements, so a run can be read next to the document rather than trusted. The
trace is data, not printed output — the CLI renders it, tests assert on it, and neither can
drift from what the engine actually did because both read the same object.

A step also carries ``expected``. Where the framework publishes a number — section 18's bid
ladder, Appendix C's utilities — the trace records the published value beside the computed
one. That turns "the run finished" into "the run agreed with the document", which is a
different and much more useful claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class Step:
    """One stage of the pipeline, tied to the section that specifies it."""

    key: str
    section: str
    title: str
    rows: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()
    #: Published value -> computed value, where the framework states one.
    expected: Mapping[str, tuple[Any, Any]] = field(default_factory=dict)

    @property
    def checked(self) -> bool:
        return bool(self.expected)

    @property
    def agrees(self) -> bool:
        """True when every published value matches, within display tolerance.

        Absence of a published value is not agreement — :attr:`checked` distinguishes
        "matches the document" from "the document says nothing here".
        """
        return all(_close(want, got) for want, got in self.expected.values())

    @property
    def disagreements(self) -> tuple[str, ...]:
        return tuple(
            name for name, (want, got) in self.expected.items() if not _close(want, got)
        )


def _close(want: Any, got: Any, tolerance: float = 0.05) -> bool:
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(float(want) - float(got)) <= tolerance
    return want == got


class StepTrace:
    """An ordered, append-only record of the run."""

    def __init__(self) -> None:
        self._steps: list[Step] = []

    def add(
        self,
        key: str,
        section: str,
        title: str,
        rows: Sequence[tuple[str, str]] = (),
        notes: Sequence[str] = (),
        expected: Mapping[str, tuple[Any, Any]] | None = None,
    ) -> Step:
        step = Step(
            key=key,
            section=section,
            title=title,
            rows=tuple(rows),
            notes=tuple(notes),
            expected=dict(expected or {}),
        )
        self._steps.append(step)
        return step

    def __iter__(self) -> Iterator[Step]:
        return iter(self._steps)

    def __len__(self) -> int:
        return len(self._steps)

    def __getitem__(self, key: str) -> Step:
        for step in self._steps:
            if step.key == key:
                return step
        raise KeyError(f"no step {key!r}; ran: {[s.key for s in self._steps]}")

    @property
    def steps(self) -> tuple[Step, ...]:
        return tuple(self._steps)

    @property
    def checked(self) -> tuple[Step, ...]:
        """Steps where the framework publishes a number to compare against."""
        return tuple(s for s in self._steps if s.checked)

    @property
    def failures(self) -> tuple[Step, ...]:
        return tuple(s for s in self._steps if s.checked and not s.agrees)

    @property
    def agrees(self) -> bool:
        return not self.failures
