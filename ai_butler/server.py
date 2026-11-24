"""ASGI 入口（统一启动点）。"""

from __future__ import annotations

import redis.asyncio as aioredis

from ai_butler.app import create_app
from ai_butler.core.config import load_settings
from ai_butler.core.environments import validate_runtime


def build_app():
    """uvicorn 工厂入口：uvicorn ai_butler.server:build_app --factory"""
    settings = load_settings()
    validate_runtime(
        settings.environment,
        postgres_port=settings.postgres_port,
        postgres_db=settings.postgres_db,
        redis_port=settings.redis_port,
    )
    client = aioredis.Redis(
        host=settings.redis_host, port=settings.redis_port, db=settings.redis_db,
        decode_responses=True,
    )
    return create_app(settings, client)
