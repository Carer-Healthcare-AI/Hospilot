"""Resource profiles. Importing this package registers every known resource type.

The bed family, ordered as the care ladder in ``config/rules/units.yaml`` orders it:
``icu > hdu > pacu > resus > ed > ward``. Every one is auctionable — which bed a hospital
actually frees is not known in advance.

Only ``icu_bed`` carries fitted-for-purpose configuration. The other five inherit ICU's caps
and TTLs and say so, both in their ``notes`` and through ``Config.unsigned``, which reports
their caps status on every run.
"""

from allocation.profiles.bed import BED_COMPONENTS, bed_profile, caps_filename
from allocation.profiles.ed_bed import ED_BED
from allocation.profiles.hdu_bed import HDU_BED
from allocation.profiles.icu_bed import ICU_BED
from allocation.profiles.pacu_bed import PACU_BED
from allocation.profiles.registry import REGISTRY, ProfileRegistry, ResourceProfile
from allocation.profiles.resus_bed import RESUS_BED
from allocation.profiles.ward_bed import WARD_BED

__all__ = [
    "BED_COMPONENTS",
    "ED_BED",
    "HDU_BED",
    "ICU_BED",
    "PACU_BED",
    "REGISTRY",
    "RESUS_BED",
    "WARD_BED",
    "ProfileRegistry",
    "ResourceProfile",
    "bed_profile",
    "caps_filename",
]
