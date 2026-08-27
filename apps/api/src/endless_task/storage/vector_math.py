"""R5.8 向量工具：float32 BLOB 编解码与余弦相似度（纯标准库，无 numpy 依赖）。"""

from __future__ import annotations

import array
import math
from typing import List, Sequence


def pack_vector(vector: Sequence[float]) -> bytes:
    """float32 小端 BLOB；空向量非法。"""
    if not vector:
        raise ValueError("Vector must not be empty.")
    packed = array.array("f", (float(value) for value in vector))
    return packed.tobytes()


def unpack_vector(blob: bytes) -> List[float]:
    if not blob or len(blob) % 4 != 0:
        raise ValueError("Invalid vector blob.")
    unpacked = array.array("f")
    unpacked.frombytes(blob)
    return list(unpacked)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = math.fsum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(math.fsum(a * a for a in left))
    norm_right = math.sqrt(math.fsum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)
