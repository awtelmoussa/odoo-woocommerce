"""
Distributed cooldown / dedupe lock backed by Redis.

Why Redis and not an in-memory dict:
  - A module-level dict lives inside ONE worker process. The moment you run
    `uvicorn --workers N` (or gunicorn), each worker keeps its own dict and
    duplicate webhooks slip through.
  - A dict also grows forever (every id ever seen stays in RAM) -> slow leak.

Redis `SET key value NX EX ttl` is atomic and self-expiring, so it is both
thread-safe across workers and leak-free.

Graceful degradation:
  If Redis is unreachable we DO NOT crash the webhook. We log and *allow* the
  sync to proceed (fail-open). Dropping a legitimate product update is worse
  than occasionally processing a duplicate.
"""

import logging

import redis.asyncio as redis

from config import REDIS_URL

logger = logging.getLogger("cooldown")

# Connection pool is created once and reused. decode_responses keeps return
# values as str instead of bytes.
_redis_client: redis.Redis = redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=2.0,
    socket_timeout=2.0,
)


async def acquire_cooldown(key: str, ttl_seconds: int) -> bool:
    """
    Try to claim a cooldown slot for `key`.

    Returns:
        True  -> slot acquired, caller SHOULD proceed.
        False -> still cooling down, caller SHOULD skip.

    Fail-open: if Redis errors, returns True (proceed) and logs the failure.
    """
    try:
        # nx=True  -> only set if it does not already exist
        # ex=ttl   -> auto-expire after ttl seconds
        was_set = await _redis_client.set(
            name=f"cooldown:{key}",
            value="1",
            nx=True,
            ex=ttl_seconds,
        )
        return bool(was_set)
    except Exception:
        logger.exception("Redis cooldown check failed for key=%s; failing open", key)
        return True


async def ping_redis() -> bool:
    """Return True if Redis responds to PING, False otherwise."""
    try:
        return bool(await _redis_client.ping())
    except Exception:
        logger.exception("Redis ping failed")
        return False


async def close_redis() -> None:
    """Close the Redis connection pool on shutdown."""
    try:
        await _redis_client.aclose()
    except Exception:
        logger.exception("Error closing Redis connection")