"""Generic resource-profile machinery. Shared infrastructure — no use case lives here.

:class:`~allocation.profiles.registry.ResourceProfile` describes *what is being auctioned*;
:class:`~allocation.profiles.registry.ProfileRegistry` holds one profile per resource type.
Both are use-case agnostic: the bed family registers itself from
``allocation.use_cases.bed.profiles``, and importing this package registers nothing.

Registration therefore happens at the composition root (``allocation.api``, ``allocation.cli``),
never from a core layer. A layer that needs a profile is handed one.
"""

from allocation.profiles.registry import (
    REGISTRY,
    ProfileRegistry,
    ResourceProfile,
    UseCaseMatcher,
    normalise,
)

__all__ = [
    "REGISTRY",
    "ProfileRegistry",
    "ResourceProfile",
    "UseCaseMatcher",
    "normalise",
]
