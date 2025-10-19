import time
import redis.asyncio as redis

class TokenBucket:
    """
    Redis-backed token bucket with two APIs:
      - allow(key, rate, capacity, cost=1.0) -> bool
      - grant(key, rate, capacity, want) -> int
    """

    def __init__(self, r: redis.Redis):
        self.r = r

    @staticmethod
    def key(stream_id: int, kind: str) -> str:
        return f"rl:{stream_id}:{kind}"

    @staticmethod
    def _to_float(val) -> float:
        if val is None:
            return 0.0
        if isinstance(val, (bytes, bytearray)):
            s = val.decode("utf-8", "ignore")
        else:
            s = str(val)
        try:
            # пусті рядки, "nan", "inf" → трактуємо як 0.0
            x = float(s)
            if x != x or x == float("inf") or x == float("-inf"):  # NaN/Inf guard
                return 0.0
            return x
        except Exception:
            return 0.0

    async def _load(self, key: str):
        now = time.time()
        data = await self.r.hgetall(key)
        tokens = self._to_float(data.get(b"tokens"))
        ts = self._to_float(data.get(b"ts"))
        return tokens, ts, now

    async def _save(self, key: str, tokens: float, now: float, rate: float, capacity: float):
        tokens = max(0.0, min(capacity, tokens))
        await self.r.hset(key, mapping={"tokens": tokens, "ts": now})
        ttl = int(max(30, (capacity / max(0.001, rate)) * 10)) if rate > 0 else 3600
        await self.r.expire(key, ttl)

    async def allow(self, key: str, rate: float, capacity: float, cost: float = 1.0) -> bool:
        tokens, ts, now = await self._load(key)

        if ts == 0.0:
            tokens = capacity
            ts = now

        elapsed = max(0.0, now - ts)
        tokens = min(capacity, tokens + rate * elapsed)

        allowed = tokens >= cost
        if allowed:
            tokens -= cost

        await self._save(key, tokens, now, rate, capacity)
        return allowed

    async def grant(self, key: str, rate: float, capacity: float, want: float) -> int:
        want = float(max(0.0, want))
        if want == 0.0:
            return 0

        tokens, ts, now = await self._load(key)

        if ts == 0.0:
            tokens = capacity
            ts = now

        elapsed = max(0.0, now - ts)
        tokens = min(capacity, tokens + rate * elapsed)

        grant_amt = min(want, tokens)
        tokens -= grant_amt

        await self._save(key, tokens, now, rate, capacity)
        return int(grant_amt)
