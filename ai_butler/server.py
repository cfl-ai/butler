"""ASGI 入口（统一启动点）。

计划书依据
----------
- 三、P0-1「部署阿里云 ECS 云原生容器环境，统一 Dockerfile、资源限制、启动模板」
- 三、P0-1「搭建四环境隔离体系：开发、测试、预发布、生产完全独立隔离」

启动时先跑环境守卫：进程连的资源必须属于它声明的环境（防「开发配置连生产库」）。
"""

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
