"""版本向量：按资源计数的设备版本向量比较。"""

from __future__ import annotations

from typing import Dict

Vector = Dict[str, int]


def compare(a: Vector, b: Vector) -> int:
    """比较两个版本向量（缺失维度视为 0）。

    返回:
        1  若 a 因果覆盖 b（各维 >= 且至少一维 >）
        -1 若 b 因果覆盖 a
        0  若两者相等
        2  若并发（互不覆盖）
    """
    a_ge = True  # a 各维 >= b
    b_ge = True  # b 各维 >= a
    for key in a.keys() | b.keys():
        av = a.get(key, 0)
        bv = b.get(key, 0)
        if av < bv:
            a_ge = False
        elif av > bv:
            b_ge = False
        if not a_ge and not b_ge:
            return 2
    if a_ge and b_ge:
        return 0
    return 1 if a_ge else -1


def dominates(a: Vector, b: Vector) -> bool:
    """a 是否因果覆盖 b（严格：各维 >= 且至少一维 >）。"""
    return compare(a, b) == 1


def merge(a: Vector, b: Vector) -> Vector:
    """逐维取最大值，返回新向量。"""
    out = dict(a)
    for key, value in b.items():
        if value > out.get(key, 0):
            out[key] = value
    return out
