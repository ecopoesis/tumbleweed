"""Constants the broker tests share."""

import datetime as dt

from tumbleweed.core.types import ContentHash

NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.UTC)
HASH_A = ContentHash("a" * 64)
HASH_B = ContentHash("b" * 64)
