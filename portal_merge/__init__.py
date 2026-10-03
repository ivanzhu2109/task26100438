"""门户元数据离线合并库。"""

from .engine import (
    Candidate,
    ConfirmationRejected,
    MergeEngine,
    ResourceState,
)
from .vectors import Vector, compare, dominates, merge

__all__ = [
    "Candidate",
    "ConfirmationRejected",
    "MergeEngine",
    "ResourceState",
    "Vector",
    "compare",
    "dominates",
    "merge",
]
