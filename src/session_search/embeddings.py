from __future__ import annotations

import hashlib
import math
import struct
from array import array
from dataclasses import dataclass

from .text import tokenize


DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
HASH_BACKEND = "hash-v1"


@dataclass(slots=True)
class EmbeddingBatch:
    backend: str
    dim: int
    vectors: list[list[float]]


class Embedder:
    def __init__(self, backend: str = "auto", model: str = DEFAULT_MODEL, dim: int = 384) -> None:
        self.requested_backend = backend
        self.model = model
        self.dim = dim
        self._model_obj = None
        self.backend = HASH_BACKEND

        if backend in {"auto", "sentence-transformers"}:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                self._model_obj = SentenceTransformer(model)
                self.backend = f"sentence-transformers:{model}"
            except Exception:
                if backend == "sentence-transformers":
                    raise

    def embed(self, texts: list[str]) -> EmbeddingBatch:
        if self._model_obj is not None:
            vectors = self._model_obj.encode(  # type: ignore[union-attr]
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            rendered = [[float(x) for x in row] for row in vectors]
            dim = len(rendered[0]) if rendered else 0
            return EmbeddingBatch(self.backend, dim, rendered)
        return EmbeddingBatch(HASH_BACKEND, self.dim, [hash_embedding(text, self.dim) for text in texts])


def hash_embedding(text: str, dim: int = 384) -> list[float]:
    vector = [0.0] * dim
    tokens = tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector


def pack_vector(values: list[float]) -> bytes:
    return array("f", values).tobytes()


def unpack_vector(blob: bytes, dim: int) -> list[float]:
    if not blob:
        return []
    count = len(blob) // 4
    if count != dim:
        try:
            return list(struct.unpack(f"{count}f", blob))
        except struct.error:
            return []
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    count = min(len(left), len(right))
    return sum(left[i] * right[i] for i in range(count))
