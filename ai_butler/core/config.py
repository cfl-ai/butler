"""P0-1 统一配置装载。

计划书依据
----------
- 三、P0-1「搭建数据库抽象层：统一连接池、事务、读写分离、加密存储」
- 三、P0-1「会话管理中间件：会话 Token 生成、校验、自动刷新；设置会话有效期，
  长时间无交互会话自动失效；失效后客户端提示重新登录」
- 三、P0-1「建设统一 LLM 网关：多模型兼容、鉴权、限流、重试、负载均衡、流式转发」
- 八、系统全局核心配置参数（Jev 决策模型配置 4 项）

计划书未定义项的处理台账（Skill-05）
----------------------------------
【文档未定义项 #12】计划书原文：「设置会话有效期，长时间无交互会话自动失效」——
计划书**未给出任何数值**。按 Skill-05，AI 不得自行填补空白。

本项的补充来源为**人工授权**：用户于 2026-10-16 指示「按设计经验取值，
后期可根据实际情况调整」。该指示构成 Skill-05 要求的「人工需求补充」，
因此本模块以 PARAM_PROVENANCE 登记取值、理由与授权来源，
并把最终数值交给环境变量承载（便于后期按实际情况调整，无需改代码）。

【文档未定义项 #13】「加盐哈希」未指定算法与代价因子，
   算法选型见 app/core/security.py（实现层决定）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_butler.core.naming import env as env_name

#: 计划书未定义、经**人工授权**确定取值的参数登记表。
#: 用途：任何数值都能回溯到「计划书原文 → 未定义 → 授权来源 → 取值理由」。
PARAM_PROVENANCE: dict[str, dict[str, Any]] = {
    "session_idle_timeout_seconds": {
        "env": "SESSION_IDLE_TIMEOUT_SECONDS",
        "plan_quote": "设置会话有效期，长时间无交互会话自动失效（三、P0-1）；"
                      "会话登录管控访问权限，长时间未操作系统强制重新登录（一、项目概述）",
        "status": "人工授权取值（非计划书定义）",
        "authorized_at": "2026-10-16",
        "value": 7200,
        "rationale": "2 小时无任何交互即失效。计划书规定会话失效**直接终止 Graph 流程**，"
                     "阈值必须覆盖一次完整任务周期（规划→质检→三档预算→用户确认→流式播报），"
                     "过短会在流程中途切断会话；2 小时仍满足「长时间未操作强制重新登录」。",
        "tunable": True,
    },
    "session_ttl_seconds": {
        "env": "SESSION_TTL_SECONDS",
        "plan_quote": "设置会话有效期（三、P0-1）",
        "status": "人工授权取值（非计划书定义）",
        "authorized_at": "2026-10-16",
        "value": 43200,
        "rationale": "单个会话凭证最长 12 小时，覆盖一个自然使用日；"
                     "持续使用时在凭证最后 30 分钟内由会话鉴权中间件无感换发，用户不会被打断；"
                     "跨天强制轮换凭证，与「切换账号自动清理旧账号本地缓存」形成纵深。",
        "tunable": True,
    },
    "session_refresh_ahead_seconds": {
        "env": "SESSION_REFRESH_AHEAD_SECONDS",
        "plan_quote": "会话 Token 生成、校验、自动刷新（三、P0-1）",
        "status": "实现参数",
        "authorized_at": "2026-10-16",
        "value": 1800,
        "rationale": "凭证最后 30 分钟内的任意请求换发新凭证，保证「自动刷新」真正无感；"
                     "原 300 秒相对 12 小时凭证过窄，容易错过刷新窗口。",
        "tunable": True,
    },
}

#: 设计不变式：空闲阈值必须严于凭证有效期，否则空闲控制形同虚设。
SESSION_PARAM_INVARIANTS = (
    "session_idle_timeout_seconds < session_ttl_seconds",
    "0 < session_refresh_ahead_seconds < session_ttl_seconds",
)


class MissingAuthorizedParamError(RuntimeError):
    """人工授权参数缺失。数值来源见 PARAM_PROVENANCE，需在环境变量中提供。"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 配置文件位置可用命名规范定义的变量覆盖（默认 .env，位于项目根）
        # 默认指向 dev；测试夹具强制 .env.test，compose 通过 AIBUTLER_ENV 指向容器内文件。
        # 四环境文件由 environments.Environment.env_file 登记，不存在共用的 .env。
        env_file=os.environ.get(env_name("ENV"), ".env.dev"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 四环境隔离（三、P0-1「开发、测试、预发布、生产完全独立隔离」）----------
    # 该字段必须与所连资源一致，由 environments.validate_runtime 在启动时守卫
    environment: str = "dev"

    # ---------- 数据库抽象层（三、P0-1「统一连接池、事务、读写分离、加密存储」）----------
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    # 读写分离：未配置只读库时复用主库（本地开发形态，四环境隔离时按环境拆分）
    postgres_read_host: str | None = None
    postgres_read_port: int | None = None
    postgres_pool_size: int = 10
    postgres_max_overflow: int = 20
    postgres_pool_timeout: int = 30
    postgres_echo: bool = False
    # 「隐私字段独立加密存储」所需密钥（实现参数，非计划书参数）
    field_encryption_key: str = ""

    # ---------- Redis：会话与限流载体 ----------
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0

    # ---------- 会话（三、P0-1）----------
    # 三项均为人工授权/实现参数，取值与理由见 PARAM_PROVENANCE；
    # 不设代码默认值，强制由环境变量显式提供，避免「静默沿用旧值」。
    session_token_secret: str
    session_ttl_seconds: int
    session_idle_timeout_seconds: int
    session_refresh_ahead_seconds: int

    # ---------- 统一 LLM 网关（三、P0-1「多模型兼容…流式转发」）----------
    llm_provider_order: str = "ollama"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_rate_limit_per_minute: int = 60           # 「限流」额度（实现参数）
    jev_rate_limit_per_minute: int = 30           # 「Jev 调用独立配额统计」（实现参数）

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""

    # ---------- 图文识别 / 图文输出（台账 U61/U62，**计划书未定义**）----------
    # 计划书 P7「双输入」只写了语音 + 文字；图片输入与图文输出由人工指令
    # （2026-10-24「支持图文识别和输出」）追加，按「开源借鉴」处理。
    # 默认顺序 ollama → openai：前者离线可查（模型没拉取能被 /api/tags 发现），
    # 后者用于接入任意 OpenAI 兼容的多模态端点。
    vision_provider_order: str = "ollama,openai"
    vision_timeout_seconds: float = 120.0          # 视觉推理比纯文本慢，单独给超时
    ollama_vision_model: str = "qwen2.5vl:3b"      # 中文 OCR 可用；moondream 实测中文无效
    openai_vision_base_url: str = ""
    openai_vision_api_key: str = ""
    openai_vision_model: str = ""

    # ---------- 语音链路（P7；模型落地方式见台账 U65）----------
    # 计划书 P7 要求 VAD/ASR/TTS；U65 明确「识别在云端执行、端侧模型可选」，
    # 因此服务端必须有可跑的一整套模型，且缺模型时 fail-closed 而不是返回空文本。
    speech_models_root: str = "models/speech"
    speech_enable: bool = True
    speech_max_audio_seconds: float = 60.0      # 单次上传音频上限（实现参数）
    speech_tts_speaker: int = 0                  # VITS-zh 多说话人：0 号默认女声
    speech_tts_speed: float = 1.0                # 0.5~2.0 之外直接拒绝（音质会明显劣化）
    speech_num_threads: int = 2                  # 单进程推理线程数
    voiceprint_threshold: float = 0.35           # U63：声纹判等阈值，偏严
    voiceprint_max_samples: int = 3              # 每账号最多保留的声纹样本数

    # ---------- 访客模式（台账 U64，**计划书未定义**）----------
    # 「未登录只能文字查询」由人工授权扩宽了可用面，但**没有放宽鉴权**：
    # 访客仍持短期凭据、仍过同一套中间件，只是能力表里只有 text_query。
    guest_enable: bool = True
    guest_session_ttl_seconds: int = 3600        # 访客凭据有效期，短于成员
    guest_text_query_budget: int = 30            # 访客问答次数上限（成本闸）

    # ---------- 文字查询链路（检索 + 复检，U66）----------
    search_reranker_enable: bool = False         # bge-reranker-base 约 1GB；关时如实报 reranked=False
    chat_verify_enable: bool = True              # 回答前复检（P6 的口径前置到问答链路）
    chat_top_k: int = 5
    guest_public_scope: str = "public-workspace"  # 访客检索用的公共作用域标识

    # ---------- 多轮上下文（U73，计划书三、P3「会话上下文管理」）----------
    # 关掉即全局退化为单轮；**默认开**，因为「多轮」是计划书点名要求的能力，
    # 默认关等于把它做成一个需要用户自己发现的开关。
    chat_history_enable: bool = True
    chat_history_max_turns: int = 20             # 窗口内最多保留的轮数
    chat_history_max_chars: int = 4000           # 窗口总字符上限（压缩触发线）
    chat_history_keep_turns: int = 6             # 压缩时无条件保留的最近轮数
    chat_summary_max_chars: int = 600            # 摘要上限
    conversation_ttl_seconds: int = 604800       # 索引项保留 7 天

    # ---------- 五层意图路由接线（U74，计划书三、P3）----------
    # 五层里 L1 硬规则 / L2 Jev / L3 向量**有实现并已接线**；
    # L4 垂类模型（U110，落地 U98 协议）**已有实现但默认不接线**——
    #   scheduler/vertical.py 给出了首个 `VerticalDecisionModel` 实现，标定守护全绿前
    #   不进主路由（避免未标定的阈值污染线上分发）；
    # L5 与 L2 是同一件事（同网关同 prompt 形态），接了两份账 —— 详见 scheduler/intent_wiring.py。
    intent_router_enable: bool = True
    intent_vector_enable: bool = True
    # 0.62 **不是标定值**：它是本层既有默认值，且在 62 句自建语料上实测优于 0.80/0.85
    # （总准 83.9% vs 77.4%）。但那次实测的示例语料与测试句同源，L3 成绩被高估；
    # U03 要求的中文验证集标定**未完成**，所以这里留成可调数字，不写成定论。
    intent_vector_threshold: float = 0.62

    # ---- 第 4 层：垂类专用决策模型（U110，落地 U98）----
    # 默认**不接线**进主路由：L4 的阈值尚未在人工标注验证集上标定
    # （VERTICAL_THRESHOLD_CALIBRATED=False，与 L3 同纪律），标定守护全绿后再打开。
    # 打开后 L4 在 L3 之后接入（保持五层冻结顺序的子序列）。
    vertical_router_enable: bool = False
    # 与 scheduler.router.DEFAULT_VERTICAL_THRESHOLD 对齐；同样**未标定**，可调数字。
    vertical_threshold: float = 0.55

    # ---- 账号体系（U67 / U68 / U69）----
    # 账号文件放哪：空则跟 storage_root 走，独立配置是为了让隐私数据能单独挂卷
    account_store_root: str = ""
    # 重置令牌投递通道：dev（开发，响应回显）/ console（打日志）/ smtp（真投递）
    # **production 环境只允许 smtp**，其余在启动时直接拒绝（U68 fail-closed）
    reset_channel: str = "dev"

    # ---- 重置令牌的邮件通道（U72）----
    # 只对**邮箱注册**的账号可用；手机号账号没有收件地址，smtp 通道会如实记一条 skip。
    # 缺 host/port/sender 时启动即报 channel_not_configured，不会带着半个配置上线。
    smtp_host: str = ""
    smtp_port: int = 465                          # 465=SSL，587=STARTTLS；用 SMTP_SSL 开关区分
    smtp_sender: str = ""                         # 发件地址（多为与 SMTP_USER 同一个邮箱）
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_ssl: bool = True

    # ---- 模型接入（U70）----
    model_free_daily_calls: int = 50              # 免费档每账号每日上限
    model_trial_daily_calls: int = 100            # 限免档每账号每日上限

    # ---- 定位（U71）----
    geo_grid_decimals: int = 3                    # 坐标粗化位数，3 ≈ 110m
    geo_recommend_limit: int = 5                  # 推荐条数上限

    # ---- 高德 Web 服务（U108，用户 2026-10-26 提供）----
    # 空 = 走拟人化旁路（official=False）；填入官方 key 即改走 restapi.amap.com
    # （official=True，镇/乡 township / 周边 POI / 官方 SLA 可用）。秘密，仅存于 .env.*。
    amap_key: str = ""

    # ---- 实时天气（U96/U97）----
    weather_ttl_seconds: float = 600.0            # 缓存 TTL；超期即重新采集
    #: 请求 Open-Meteo 用时区 UTC——`current.time` 原样成为 fetched_at，
    #: 带时区偏移的本地时间会让「距现在多久」的算术差出整个偏移量
    weather_timezone: str = "UTC"
    weather_timeout: float = 10.0                 # 采集超时（秒）

    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"

    # ---------- Agent-Wiki v2（八、系统全局核心配置参数，数值不得改动）----------
    wiki_public_workspace_enable: bool = True      # wiki_public_workspace_enable = true
    wiki_user_vault_per_user: bool = True          # wiki_user_vault_per_user = true
    wiki_compile_auto_run: bool = True             # wiki_compile_auto_run = true
    wiki_auto_report_contradiction: bool = True    # wiki_auto_report_contradiction = true
    wiki_auto_report_low_confidence: bool = True   # wiki_auto_report_low_confidence = true
    wiki_max_digest_size: int = 12000              # wiki_max_digest_size = 12000
    # 存储根目录（实现参数，非计划书参数）
    wiki_root: str = "data/dev/agent_wiki"
    # 统一文件存储根（三、P0-1「统一文件存储、日志存储、版本备份、项目回溯机制」）
    storage_root: str = "data/dev/storage"

    # ---------- 全局中间件（三、P0-1「接口熔断、超时、降级、防雪崩机制」）----------
    # 以下均为**实现参数**（负载相关），非计划书【八】固化参数，可按实际负载调整
    request_timeout_seconds: float = 30.0            # 接口超时；流式响应按「首字节截止」处理
    circuit_breaker_failure_threshold: int = 5       # 连续失败达阈值即熔断
    circuit_breaker_cooldown_seconds: int = 30       # 熔断冷却时长，之后半开探测
    circuit_breaker_half_open_max_calls: int = 1     # 半开态允许的探测并发
    bulkhead_max_concurrency: int = 8                # 单依赖最大并发（防雪崩）
    bulkhead_acquire_timeout_seconds: float = 5.0    # 并发闸等待上限
    log_level: str = "INFO"

    # ---------- Jev 决策模型（八、系统全局核心配置参数，数值不得改动）----------
    jev_enable: bool = True                       # jev_enable = true
    jev_confidence_threshold: float = 0.82        # jev_confidence_threshold = 0.82
    jev_fallback_enable: bool = True              # jev_fallback_enable = true
    jev_shadow_mode: bool = True                  # jev_shadow_mode = true

    # ---- 以下两项是**实现参数**，不属于【八】冻结集合，可调 ----
    # E-78：64 token 会被弱模型「复述候选标签」吃掉，JSON 写到一半被截断。
    # 本机 3B 实测 64→256 后 3/6 成功变 6/6；云端 deepseek-chat 实测 64 也不截断（62/62）。
    # 因此 256 不是修 bug，是**给弱模型留余量**：成本可忽略，没必要卡在边界上。
    jev_max_tokens: int = 256
    # E-80：**默认采信**模型的澄清请求（JEV-003 是冻结不变量，偏离要有证据）。
    # 实测对比：
    #   · 本机 qwen2.5vl:3b —— 6 次澄清 6 次错（把「信息不足」当成「多意图」），
    #     关掉后 A 方案从 69.4% 升到 79.0%，那时关掉是对的；
    #   · 云端 deepseek-chat —— 16/62 要求澄清，其中 13 条的标签本来就对，
    #     且集中在 multi_intent(7/7) 与 unknown(6/6)，正是**该问**的两类；
    #     关掉只多拿 1 个用例（+1.6 点，落在 62 句语料的噪声内），
    #     不足以作为偏离冻结不变量的理由。
    # 结论：默认 True；**若把 L2 换成弱模型，请设 JEV_ALLOW_CLARIFY=false**。
    jev_allow_clarify: bool = True

    @field_validator("environment")
    @classmethod
    def _known_environment(cls, value: str) -> str:
        from ai_butler.core.environments import ENV_NAMES

        if value not in ENV_NAMES:
            raise ValueError(f"environment 必须是 {ENV_NAMES} 之一，实际 {value!r}")
        return value

    @property
    def provider_order(self) -> list[str]:
        return [p.strip().lower() for p in self.llm_provider_order.split(",") if p.strip()]

    @property
    def vision_provider_order_list(self) -> list[str]:
        return [p.strip().lower() for p in self.vision_provider_order.split(",") if p.strip()]

    @property
    def write_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def read_dsn(self) -> str:
        """读写分离：未配置只读库时复用主库。"""
        if not self.postgres_read_host:
            return self.write_dsn
        port = self.postgres_read_port or self.postgres_port
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_read_host}:{port}/{self.postgres_db}"
        )


def load_settings(**overrides: Any) -> Settings:
    """装载配置；人工授权参数缺失时明确报错并指出环境变量名。"""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        bad = {e["loc"][0] for e in exc.errors() if e.get("loc")}
        missing = [
            f"{PARAM_PROVENANCE[name]['env']}（{name}）"
            for name in PARAM_PROVENANCE
            if name in bad
        ]
        if missing:
            raise MissingAuthorizedParamError(
                "会话参数缺失：" + "、".join(missing) + "\n"
                "这些数值计划书未定义，现由人工授权取值（授权日期 2026-10-16）。\n"
                "取值、理由与计划书原文见 app/core/config.py 的 PARAM_PROVENANCE；\n"
                "请写入 .env（可参照 .env.example），或按实际情况调整后再写入。"
            ) from exc
        raise


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
