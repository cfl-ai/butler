"""四环境隔离体系。

计划书依据
----------
- 三、P0-1「搭建四环境隔离体系：开发、测试、预发布、生产完全独立隔离」
- 三、P0-1「部署阿里云 ECS 云原生容器环境，统一 Dockerfile、资源限制、启动模板」

隔离口径（「完全独立隔离」的落地含义）
------------------------------------
四环境之间**不共享任何资源**：宿主端口、数据库名、数据目录、容器名、项目名、配置与密钥文件
两两不相交。本模块是这一口径的唯一真值源，由 assert_disjoint() 与运行时守卫共同保证。
"""

from __future__ import annotations

from dataclasses import dataclass

ENV_NAMES: tuple[str, ...] = ("dev", "test", "staging", "prod")


class EnvironmentIsolationError(RuntimeError):
    """跨环境串用资源。计划书要求四环境完全独立隔离。"""


@dataclass(frozen=True)
class Environment:
    name: str
    env_file: str          # 该环境的唯一配置与密钥来源
    postgres_port: int
    redis_port: int
    postgres_db: str
    data_root: str
    container_prefix: str
    project_name: str
    exposes_host_ports: bool     # 仅本地环境绑回环端口，预发布/生产不发布

    @property
    def data_plane_internal(self) -> bool:
        """内网服务层是否严格无外网出口。

        本地必须为 False：Docker 不会为「只挂 internal 网络」的容器完成端口转发
        （实测见 MEMORY.md E-07），而本地开发/测试需要宿主可达数据面。
        预发布与生产为 True，且不发布任何宿主端口。
        """
        return not self.exposes_host_ports

    @property
    def api_port(self) -> int:
        return 8400 + ENV_NAMES.index(self.name)


ENVIRONMENTS: dict[str, Environment] = {
    "dev": Environment("dev", ".env.dev", 5434, 6381, "aibutler_dev",
                       "data/dev", "aibutler-dev", "ai-butler-dev", True),
    "test": Environment("test", ".env.test", 5435, 6382, "aibutler_test",
                        "data/test", "aibutler-test", "ai-butler-test", True),
    "staging": Environment("staging", ".env.staging", 5436, 6383, "aibutler_staging",
                           "data/staging", "aibutler-staging", "ai-butler-staging", False),
    "prod": Environment("prod", ".env.prod", 5437, 6384, "aibutler_prod",
                        "data/prod", "aibutler-prod", "ai-butler-prod", False),
}


def get(name: str) -> Environment:
    try:
        return ENVIRONMENTS[name]
    except KeyError as exc:
        raise EnvironmentIsolationError(
            f"未知环境 {name!r}，允许：{ENV_NAMES}（计划书三、P0-1 四环境隔离）"
        ) from exc


def assert_disjoint() -> None:
    """四环境不得共享任何资源。任一维度出现重复即视为隔离被破坏。"""
    dims = {
        "宿主 PostgreSQL 端口": lambda e: e.postgres_port,
        "宿主 Redis 端口": lambda e: e.redis_port,
        "API 端口": lambda e: e.api_port,
        "数据库名": lambda e: e.postgres_db,
        "数据目录": lambda e: e.data_root,
        "容器名前缀": lambda e: e.container_prefix,
        "Compose 项目名": lambda e: e.project_name,
        "配置文件": lambda e: e.env_file,
    }
    for label, pick in dims.items():
        seen: dict[object, str] = {}
        for env in ENVIRONMENTS.values():
            value = pick(env)
            if value in seen:
                raise EnvironmentIsolationError(
                    f"{label} 冲突：{seen[value]} 与 {env.name} 都使用 {value!r}"
                )
            seen[value] = env.name


def validate_runtime(environment: str, *, postgres_port: int, postgres_db: str,
                     redis_port: int) -> None:
    """运行时守卫：当前进程连的资源必须属于它声明的环境。

    防的是最危险的一类事故——用开发配置连上了生产库。
    """
    env = get(environment)
    problems: list[str] = []
    if postgres_port != env.postgres_port:
        problems.append(f"PostgreSQL 端口 期望 {env.postgres_port}，实际 {postgres_port}")
    if postgres_db != env.postgres_db:
        problems.append(f"数据库名 期望 {env.postgres_db}，实际 {postgres_db}")
    if redis_port != env.redis_port:
        problems.append(f"Redis 端口 期望 {env.redis_port}，实际 {redis_port}")
    if problems:
        raise EnvironmentIsolationError(
            f"环境 {environment!r} 的资源不匹配，疑似跨环境串用：\n  - " + "\n  - ".join(problems)
        )
