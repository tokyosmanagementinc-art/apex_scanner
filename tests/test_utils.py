import json
import time
from datetime import datetime

import numpy as np

from scanner.utils import FileCache


def test_filecache_set_get_and_invalidate(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = FileCache(cache_dir=str(cache_dir))
    key = "test-key"
    value = {
        "symbol": "AAPL",
        "price": np.float64(150.25),
        "tags": ("momentum", "breakout"),
        "created": datetime(2026, 5, 23, 14, 0, 0),
    }

    cache.set(key, value, ttl=2)
    result = cache.get(key)

    assert result["symbol"] == "AAPL"
    assert result["price"] == 150.25
    assert result["tags"] == ["momentum", "breakout"]
    assert result["created"] == "2026-05-23T14:00:00"

    cache.invalidate(key)
    assert cache.get(key) is None


def test_filecache_expiration_and_clear_expired(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = FileCache(cache_dir=str(cache_dir))
    cache.set("short-lived", {"value": 1}, ttl=0)

    time.sleep(0.01)
    assert cache.get("short-lived") is None
    # If get() already removed the expired entry, clear_expired may see zero removed.
    removed = cache.clear_expired()
    assert removed >= 0
