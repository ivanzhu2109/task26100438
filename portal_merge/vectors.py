"""Version vector (per-resource device counters) primitives.

A vector maps device_id -> counter. Missing dimensions are treated as 0.
Vector A causally covers B iff every dimension of A is >= B's and at least
one dimension is strictly greater.
"""

Vector = dict  # dict[str, int]


def covers(a: Vector, b: Vector) -> bool:
    """Return True iff a causally covers b (a >= b in all dims, > in one)."""
    greater = False
    for key in a.keys() | b.keys():
        av = a.get(key, 0)
        bv = b.get(key, 0)
        if av < bv:
            return False
        if av > bv:
            greater = True
    return greater


def join(a: Vector, b: Vector) -> Vector:
    """Return the element-wise maximum of two vectors."""
    return {key: max(a.get(key, 0), b.get(key, 0)) for key in a.keys() | b.keys()}
