"""Best-effort Redis cache for authorized search candidate metadata."""

import json
import threading

from redis import Redis, RedisError


class RedisSearchCache:
    def __init__(self, url: str, *, ttl_seconds: int = 120, client: Redis | None = None,
                 key_prefix: str = "agent:search:v1:"):
        self.client = client or Redis.from_url(url, socket_connect_timeout=0.2,
                                               socket_timeout=0.2, decode_responses=True)
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self._lock = threading.Lock()
        self._counts = {"hits": 0, "misses": 0, "writes": 0, "errors": 0}

    def _count(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def get(self, key: str) -> list | None:
        try:
            value = self.client.get(key)
            if value is None:
                self._count("misses")
                return None
            parsed = json.loads(value)
            if not isinstance(parsed, list) or len(parsed) > 50:
                self._count("errors")
                return None
            self._count("hits")
            return parsed
        except (RedisError, ValueError, TypeError):
            self._count("errors")
            return None

    def set(self, key: str, candidates: list) -> None:
        try:
            self.client.set(key, json.dumps(candidates, separators=(",", ":")), ex=self.ttl_seconds)
            self._count("writes")
        except RedisError:
            self._count("errors")

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
