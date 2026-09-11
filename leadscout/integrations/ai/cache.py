"""Small in-process cache for validated AI responses."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_MAX_ITEMS = 128


def _cache_key(operation: str, payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(f"{operation}\0{serialized}".encode()).hexdigest()


class AICache:
    def __init__(self):
        self.items = OrderedDict()

    def get(self, key: str, expected_type: type[T]) -> T | None:
        cached = self.items.get(key)
        if not cached:
            return None
        created_at, value = cached
        if time.monotonic() - created_at >= CACHE_TTL_SECONDS:
            self.items.pop(key, None)
            return None
        self.items.move_to_end(key)
        return value.model_copy(deep=True) if isinstance(value, expected_type) else None

    def put(self, key: str, value: BaseModel) -> None:
        self.items[key] = (time.monotonic(), value.model_copy(deep=True))
        self.items.move_to_end(key)
        while len(self.items) > CACHE_MAX_ITEMS:
            self.items.popitem(last=False)

    def clear(self) -> None:
        self.items.clear()
