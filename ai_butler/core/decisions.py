"""未定义项裁定台账。

计划书依据
----------
- 三、P0-1「输出统一架构规范文档，全项目强制执行」——本台账是该规范文档
  「未定义项处理」章节的唯一数据来源。
- Skill-05：计划书未定义的字段、逻辑、实现方式，AI 不得自行填补空白。

两条合法的裁定路径（缺一不可登记）
--------------------------------
1. **人工授权** —— 用户直接给出取值，或明确授权按工程经验取值；
2. **开源借鉴** —— 用户指示「所有未定义项可直接检索 GitHub 中开源项目数据进行借鉴」，
   据此检索开源成熟实践并采用，逐项登记出处、License 与代码复用边界；
3. **文档推导** —— 计划书未点名该处，但其**自身已有的定义**可以唯一确定答案
   （例如 P1 已定义「公共域/私有域」的划分口径，据此可判定未被点名的表的归属）。
   此类必须写明推导所依据的原文片段，**不得用它掩盖真正的自由裁量**。

License 合规
-----------
code_reuse 字段区分两种边界：
  - "allowed"     —— 许可宽松（MIT / Apache-2.0 / BSD / CC-BY），可按许可条件复用代码与数据；
  - "design_only" —— 许可有传染性或未声明（AGPL / GPL / 无 LICENSE），**只借鉴设计，不得复制代码或数据**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

RESOLVED = "resolved"
PENDING = "pending"


@dataclass(frozen=True)
class Decision:
    key: str
    title: str
    phase: str                     # 计划书阶段：P0-1 / P2 / P3 / P7 / P8 / P10
    plan_quote: str                # 计划书原文片段（说明此处未定义）
    status: str
    source: str                    # 人工授权 / 开源借鉴 / 实现参数
    resolution: str
    decided_at: str
    references: tuple[str, ...] = ()
    license_note: str = ""          # **已采用来源**的许可
    excluded_note: str = ""         # **已排除备选**的许可警告（不影响本项的可复用性判定）
    # "allowed"（可复用代码/数据）/ "design_only"（只借设计）/ "n/a"（人工授权或实现参数，无外部来源）
    code_reuse: str = "n/a"


_D = "2026-10-16"
#: U43 起的裁定日期（接管后新增的三层权限 / 检索路由 / 复检 / Harness 评测）
_D2 = "2026-10-19"
#: U53 起的裁定日期（运行时内核补齐：权限闸接线 / 事件日志 / 上下文计量 / 超时审批）
_D3 = "2026-10-23"
#: U63~U66 语音三件套 / 访客层 / 检索分类，U67 起的账号体系与模型接入
_D4 = "2026-10-24"
#: U73 多轮上下文，U74 五层路由接线，U75 影子旁路与判定确定性，
#: U76 评测缺陷单与处置路由，U77 缺陷受理与自我修改边界
_D5 = "2026-10-25"
#: U78 大模型写补丁的安全边界，U79 复评的两个判据（同日第二批，与 U76/U77 同一链路的下一段）
_D6 = "2026-10-25"
#: U80 两个 Agent 的配合需要一个装配点与固定顺序（同日第三批：把闭环从「测试里能跑」变成「装了电」）
_D7 = "2026-10-25"
#: U81 跨轮重试的四道约束（同日第四批：把 U80 登记为「未做」的那一项补上）
_D8 = "2026-10-25"

DECISIONS: tuple[Decision, ...] = (
    Decision(
        key="U01", title="意图标签枚举清单", phase="P3",
        plan_quote="「Jev System-One 高速结构化决策层：输出主意图、候选意图列表、是否多意图冲突、"
                   "置信概率」（三、P3）；「仅输出结构化枚举标签 + 置信概率」（一、项目概述）",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="采用 home-assistant/intents 的 domain → intent → slot → slot_combinations 分层结构"
                   "与命名；中文句式按自家业务重写；降级路径复用其 no_intent 文案位。不自行发明标签体系。",
        references=("https://github.com/home-assistant/intents",),
        license_note="CC-BY-4.0（LICENSE.md 已核）；已含 zh-CN 官方句式", code_reuse="allowed",
    ),
    Decision(
        key="U02", title="Jev 决策模型本体形态", phase="P3",
        plan_quote="「Jev 决策模型接入网关」「集成 LLM 推理层」（三、P0-1）；\n"
                   "未说明 Jev 是微调小模型还是受限大模型",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="走「受限小模型」路线：Qwen2.5-0.5B/1.5B 为基座，输出用 JSON Schema 硬约束"
                   "（本机 Ollama format，或 outlines / xgrammar）。不采用「受限大模型」。",
        references=("https://github.com/QwenLM/Qwen2.5", "https://github.com/dottxt-ai/outlines",
                    "https://github.com/mlc-ai/xgrammar", "https://github.com/ollama/ollama"),
        license_note="Qwen2.5 Apache-2.0；outlines / xgrammar Apache-2.0；Ollama MIT",
        code_reuse="allowed",
    ),
    Decision(
        key="U03", title="第四层「垂类专用决策模型」", phase="P3",
        plan_quote="五层路由第 4 层「垂类专用决策模型」（三、P3）——未说明模型形态与训练方式",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="照 RouteLLM 的 bert router 形态：中文基座小分类器 + 阈值路由。"
                   "阈值必须在自建中文验证集上重新标定，不得沿用英文默认值。",
        references=("https://github.com/lm-sys/RouteLLM",),
        license_note="Apache-2.0", code_reuse="allowed",
    ),
    Decision(
        key="U04", title="关系型数据库选型", phase="P0-1",
        plan_quote="「搭建数据库抽象层：统一连接池、事务、读写分离、加密存储」（三、P0-1）——未点名产品",
        status=RESOLVED, source="人工授权", decided_at=_D,
        resolution="PostgreSQL 16 + pgvector，沿用 job-agent 已验证的部署路径。",
    ),
    Decision(
        key="U07", title="三档预算的金额阈值", phase="P8",
        plan_quote="「低/中/高预算」只有语义描述（三、P8），未给出「小额」「大额」的分界数值",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="中预算默认「单笔 ≤1000 元免密 + 单卡单日累计 ≤3000 元封顶」，锚定银联/建行现网惯例；\n"
                   "低预算阈值 = 0（逐笔人工确认）；高预算不设固定金额，改用月度预算剩余比例。"
                   "阈值引擎采用 Actual Budget 的「条件 → 动作」规则模型（amount / payee / date）。",
        references=("https://accounts2.ccb.com/chn/2025-02/24/article_2025022414432570876.shtml",
                    "https://actualbudget.org/docs/budgeting/rules/"),
        license_note="建行公告为公开规范；Actual Budget MIT",
        excluded_note="Firefly III 为 AGPL-3.0-or-later —— 只读其数据模型，不抄代码",
        code_reuse="allowed",
    ),
    Decision(
        key="U11", title="CI 平台选型", phase="P0-2",
        plan_quote="「制定 Git 分支规范、CI 自动构建、自动部署流水线」（三、P0-2）——未指定平台",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="首选 Gitea + act_runner：MIT、官方提供 Windows runner 二进制、界面中文、"
                   "Docker 与 host 双执行模式。备选阿里云效 Flow 私有构建集群（官方支持 Windows）。"
                   "排除：GitHub Actions（github.com 实测不可达）、Jenkins（本机无 JDK）、"
                   "Gitee Go（Windows 构建环境无官方文档）。",
        references=("https://docs.gitea.com/usage/actions/act-runner",
                    "https://help.aliyun.com/zh/yunxiao/user-guide/build-a-cluster"),
        license_note="Gitea MIT；阿里云效为商业 SaaS", code_reuse="allowed",
    ),
    Decision(
        key="U12", title="会话有效期与空闲阈值", phase="P0-1",
        plan_quote="「设置会话有效期，长时间无交互会话自动失效」（三、P0-1）——未给出任何数值",
        status=RESOLVED, source="人工授权", decided_at=_D,
        resolution="空闲 7200s（2 小时）失效；凭证有效期 43200s（12 小时）；刷新余量 1800s。"
                   "不变式：空闲阈值 < 凭证有效期。已登记于 config.PARAM_PROVENANCE。",
    ),
    Decision(
        key="U13", title="加盐哈希算法与代价因子", phase="P0-1",
        plan_quote="「用户账号密码加盐哈希存储，数据库不保存明文密码」（三、P0-1）——未指定算法",
        status=RESOLVED, source="实现参数", decided_at=_D,
        resolution="bcrypt，cost=12；口令先做 SHA-256 预哈希以支持任意长度输入。",
    ),
    Decision(
        key="U08", title="Harness 与 Claude Code 的边界", phase="P10",
        plan_quote="「基于 Claude Code 工业级软件工程智能体循环」与「Harness Runtime 调度」并列，关系未定义",
        status=RESOLVED, source="人工授权", decided_at=_D,
        resolution="不引入 Claude Code 本体；仅借用其设计思维、代码与循环逻辑，自研 Agentic Loop，"
                   "架构范式对齐四阶循环 + MCP + Hook + 沙箱 + 持久记忆。",
    ),
    Decision(
        key="U09", title="阿里云 ECS 实例", phase="P0-1",
        plan_quote="「部署阿里云 ECS 云原生容器环境」（三、P0-1）——规格、地域、凭据未定义",
        status=RESOLVED, source="人工授权", decided_at=_D,
        resolution="P0 阶段先纯本地（本机 PostgreSQL + Redis + Ollama + DeepSeek 全链路已验证），"
                   "ECS 待底座完成后再接。",
    ),
    Decision(
        key="U10", title="Embedding / Rerank 模型选型", phase="P2",
        plan_quote="「统一切片规则、Embedding 入库规则」「Rerank 三重混合检索」（三、P2）——未指定模型",
        status=RESOLVED, source="人工授权", decided_at=_D,
        resolution="Embedding 用 bge-small-zh-v1.5（512 维）；Rerank 用 bge-reranker-base。"
                   "本机 torch 为 CPU 版，嵌入在 CPU 推理。",
    ),
    Decision(
        key="U14", title="多意图澄清的结构化契约", phase="P3",
        plan_quote="「多意图冲突禁止系统自动选优先级，必须向用户确认」（八、multi_intent_auto_resolve=False）"
                   "——未定义模型侧如何表达「必须澄清」",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="借 CLAMBER 的字段设计：require_clarification(0/1) + clarifying_question + category，"
                   "把「禁止自动选优先级」编码成模型必出字段，而非靠后置规则判断。",
        references=("https://github.com/zt991211/CLAMBER",),
        license_note="**仓库无 LICENSE 文件（未声明）**——仅借鉴字段设计与分类法，不得分发其数据，商用前须联系作者",
        code_reuse="design_only",
    ),
    Decision(
        key="U15", title="Jev 置信概率的来源", phase="P3",
        plan_quote="「返回决策标签 + 置信概率」，阈值 jev_confidence_threshold = 0.82（三、P0-1 / 八）"
                   "——未定义置信度如何得到",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="置信概率**不得**取自约束解码的 token 概率：约束解码会 mask 掉非法 token，"
                   "概率分布被扭曲，不能作校准后的 0.82 判定依据。拆成两件事——"
                   "约束解码只保证输出格式合法；置信度由独立分类头 / 校准集（温度缩放）产出。"
                   "并借 CLINC150 的 out-of-scope 类实现「拒识 → 降级」。",
        references=("https://github.com/clinc/oos-eval",),
        license_note="CC BY 3.0", code_reuse="allowed",
    ),
    Decision(
        key="U16", title="Contradiction 矛盾建模语义", phase="P0-1",
        plan_quote="本体包含「矛盾 Contradiction」「矛盾检测」（一、三、P1）——未定义矛盾的存储语义",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="采用 Graphiti 的 fact-invalidation 语义：不删除旧声明，给旧关系写 invalid_at，"
                   "新增 contradicts 关系，保留完整时间线。",
        references=("https://help.getzep.com/graphiti/graphiti/overview",),
        license_note="Apache-2.0", code_reuse="allowed",
    ),
    Decision(
        key="U17", title="claims.jsonl / pages.json 字段约定", phase="P0-1",
        plan_quote="「同时支持人类可读 Markdown 页面 + 机器编译缓存"
                   "（agent-digest.json / claims.jsonl / pages.json）」（一、项目概述）——未定义字段",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="以 Microsoft GraphRAG 的 covariates(claim) 字段为起点：claim_id、claim_type、"
                   "description、subject_id、object_id、status(TRUE|FALSE|SUSPECTED)、"
                   "valid_from / valid_to(ISO8601)、evidence.source_text、evidence.text_unit_id、source_url。",
        references=("https://microsoft.github.io/graphrag/index/outputs/",),
        license_note="MIT", code_reuse="allowed",
    ),
    Decision(
        key="U18", title="Markdown → 机器缓存的编译流水线", phase="P0-1",
        plan_quote="「提供编译流水线，输入 Markdown 页面输出机器可读缓存，"
                   "自动检测声明、证据、矛盾、过期素材」（三、P0-1）",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="结构范式参考 Basic Memory：YAML frontmatter + 固定小节（Observations / Relations）"
                   "+ 本地索引 + 模式文件；实现自研，或参考 Apache-2.0 的 cognee。",
        references=("https://docs.basicmemory.com/raw/concepts/knowledge-format.md",
                    "https://docs.cognee.ai/llms-core.md"),
        license_note="Basic Memory **AGPL-3.0-or-later 只借鉴设计**；cognee Apache-2.0 可复用",
        code_reuse="design_only",
    ),
    Decision(
        key="U19", title="Workspace / Vault 双库的实现方式", phase="P0-1",
        plan_quote="「区分公共 Workspace（全用户共享公开知识）与用户私有 Vault（绑定 user_id）」（一）"
                   "——未定义二者是两套结构还是同一套结构加隔离",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="参考 cognee 的 NodeSet 分组：公共库与私有库共用同一套 schema，靠分组 + user_id 隔离，"
                   "不建两套结构，避免 schema 漂移。",
        references=("https://docs.cognee.ai/llms-core.md",),
        license_note="Apache-2.0", code_reuse="allowed",
    ),
    Decision(
        key="U26", title="P3 意图路由究竟几层（计划书内部表述不一致）", phase="P3",
        plan_quote="同一节里两处说法不同——核心能力写「实现升级后的**四层**意图路由」，"
                   "紧随其后的表注却写「升级后**五层**路由链路（由简单到复杂）1）…5）」"
                   "并逐条列出五层；【八】亦以「五层路由」冻结。",
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="按计划书三、P3 的**表注列出的五层**为准：同节表格是逐条枚举的明细，"
                   "比核心能力里一句概括性表述更具体，且与计划书【八】「五层路由冻结」自洽。"
                   "原文核心能力行里的「四层」应视为笔误。实现即 router.LAYERS 五层，"
                   "已由 test_five_layers_order_is_frozen 锁定顺序。"
                   "若人工另有要求（确为四层），需先改计划书【八】的冻结参数，属高危变更。",
    ),
    Decision(
        key="U27", title="P4 子 Agent 名册的阶段归属（此前的实现错位）", phase="P4",
        plan_quote="三、P4「包含子 Agent：**设备感知、天气感知、时间进度、增量抽取、"
                   "路线方案、备选预案、代码迭代 Agent**」；\n"
                   "三、P5 才是「动态事件运营 Agent」、三、P6 才是「内容质检 Agent」。",
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="按计划书原文更正阶段归属：此前把「规划 Agent」「质检 Agent」当作 P4 交付"
                   "是**错的**——计划书三、P5 才是动态事件运营 Agent（方案生成引擎），"
                   "三、P6 才是内容质检 Agent。计划书三、P4 真正要求的是 7 个感知与基础执行 Agent，"
                   "本阶段尚未实现。已实现的 registry/capabilities/facts/triggers 四件套"
                   "对应计划书三、P4「开发规范」部分，定位正确、予以保留；\n"
                   "planner/qa 两个 Agent 保留实现，但改标为 P5/P6 交付物，"
                   "待相应阶段按原文补齐其完整开发步骤。",
    ),
    Decision(
        key="U25", title="多意图澄清话术的来源（U14 与计划书的效力冲突）", phase="P3",
        plan_quote="计划书三处写明 Jev 不得输出自由文本："
                   "一、项目概述「Jev 仅输出结构化枚举标签 + 置信概率，不输出自由文本」；\n"
                   "三、P3「Jev 仅输出结构化枚举，不输出自由文本」；\n"
                   "Skill-01 同款硬限制。"
                   "而 U14 借鉴 CLAMBER 时引入的 clarifying_question 属于自由文本。",
        # 由效力顺序（计划书 ＞ Skills 集合 ＞ 统一架构规范文档 ＞ 台账裁定）唯一确定
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="按效力顺序，**计划书胜出**：Jev 只输出 require_clarification(0/1)、"
                   "clarification_category(枚举)、candidate_labels(枚举列表)，"
                   "**不输出任何询问语句**；澄清话术由路由层用枚举键模板渲染。"
                   "这样既保留 U14 的结构化「必须澄清」契约，又不违反计划书的禁自由文本。"
                   "U14 的其余部分（把禁止自动选优先级编码为模型必出字段）继续有效。",
    ),
    Decision(
        key="U24", title="知识图谱库的域归属（公共 / 私有）", phase="P1",
        plan_quote="P1 点名的四大核心数据表中，「素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID；\n"
                   "用户画像、设备状态、私有 Wiki Vault 必须绑定 user_id」——"
                   "**知识图谱库未被列入任一侧**",
        # 来源为「文档推导」而非「人工授权」：答案由计划书自身的域定义唯一确定，
        # 不涉及自由裁量，也不应冒充人工裁定。
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="按计划书「公共域：景点、天气、公开资讯，全账号可读；私有域：用户偏好、对话、消费、"
                   "设备信息、私有 Vault，严格归属 user_id」的域定义，判定知识图谱库**双域共存**："
                   "user_id 为 NULL 即公共域（景点/天气/公开资讯实体），非空即私有域（用户偏好实体）。"
                   "隔离语义：绑定账号时可见「公共域 OR 本人私有域」；未绑定账号时**只可见公共域**。"
                   "已落为 core/db.py 的 DomainScoped 并有独立用例。"
                   "备选读法（若人工另有要求）：知识图谱库整体归公共域——改继承 Base 即可，"
                   "但会丢失「用户偏好实体」的私有归属。",
    ),
    Decision(
        key="U05", title="移动端技术栈", phase="P7",
        plan_quote="「搭建独立手机 App 底层框架（纯客户端、非网页）」（三、P7）——未指定平台与框架",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="Flutter。唯一同时具备四项开源证据的栈：中文语音客户端先例（xiaozhi-flutter-client）、"
                   "端侧语音全套插件 sherpa_onnx、凭证加密存储 flutter_secure_storage、流式文本渲染。"
                   "排除 React Native（端侧 ASR/TTS 无同等成熟绑定）、KMP 与 uni-app"
                   "（均无「后台常驻唤醒 + 端侧 ASR/TTS」成熟先例；uni-app 走 WebView 音频通路，"
                   "后台常驻不可行）。原生 Kotlin 作为备选（rhasspy_mobile 先例）。",
        references=("https://github.com/xfzen/xiaozhi-flutter-client",
                    "https://pub.dev/packages/sherpa_onnx",
                    "https://github.com/mogol/flutter_secure_storage",
                    "https://github.com/Nailik/rhasspy_mobile"),
        license_note="xiaozhi-flutter-client / sherpa_onnx Apache-2.0；"
                     "flutter_secure_storage BSD-3-Clause；rhasspy_mobile MIT（备选）",
        code_reuse="allowed",
    ),
    Decision(
        key="U06", title="离线语音唤醒引擎", phase="P7",
        plan_quote="「集成后台常驻离线语音唤醒」「开发 VAD 人声检测、实时收音、随时打断机制」（三、P7）"
                   "——未定义引擎与模型",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="sherpa-onnx KWS + wenetspeech ZipFormer（int8 全套约 4.9MB）。"
                   "自定义唤醒词只需在 keywords.txt 追加拼音 token 行（「你好管家」→ n ǐ h ǎo g u ǎn j i ā），"
                   "且与 ASR / TTS / VAD 同栈，端云链路只需一套依赖。",
        references=("https://github.com/k2-fsa/sherpa-onnx",
                    "https://www.modelscope.cn/models/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"),
        license_note="sherpa-onnx 及其 KWS 模型卡均为 Apache-2.0；模型卡与仓库 LICENSE 须一并归档留证",
        excluded_note="snowboy 停更且商用需另议、Python 3.12 无法编译；"
                      "Porcupine 模型来自 Picovoice Console 商用额度需自行核对；"
                      "openWakeWord 预训练无中文，中文需自备合成语料自训",
        code_reuse="allowed",
    ),
    Decision(
        key="U20", title="流式 ASR 选型", phase="P7",
        plan_quote="「离线语音唤醒、实时收音打断、流式文字渲染」（一、端云分层规范、三、P7）——未指定 ASR",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="统一用 sherpa-onnx 流式 ZipFormer（中英双语，提供 Dart/Java/Kotlin/Swift/Python 绑定），"
                   "与唤醒词同栈。FunASR paraformer-zh-streaming 作为服务端 2pass 纠错备选。",
        references=("https://github.com/k2-fsa/sherpa-onnx", "https://github.com/modelscope/FunASR"),
        license_note="sherpa-onnx Apache-2.0（代码与模型一致）",
        excluded_note="FunASR 仓库 MIT 但模型卡为 FunASR Model License（other）——**仓库许可 ≠ 模型可用**，"
                      "商用前须读条款；SenseVoiceSmall 同为 other",
        code_reuse="allowed",
    ),
    Decision(
        key="U21", title="TTS 与「逐字渲染 + 同步播报」链路", phase="P7",
        plan_quote="「对接云端流式接口：逐字文字渲染 + 同步 TTS 播报」（三、P7）——未指定 TTS",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="云端 CosyVoice2（流式 TTS、低首包延迟）为主，端侧 sherpa-onnx VITS 兜底。"
                   "同步播报按句切分下发、客户端边收边播，才能实现「逐字渲染 + 同步 TTS」。",
        references=("https://github.com/FunAudioLLM/CosyVoice", "https://github.com/k2-fsa/sherpa-onnx",
                    "https://github.com/rany2/edge-tts"),
        license_note="CosyVoice Apache-2.0；sherpa-onnx VITS Apache-2.0",
        excluded_note="edge-tts 为混合许可（仅 srt_composer.py 是 MIT，其余 LGPLv3），"
                      "且本质调用微软 Edge 在线服务，许可与 ToS 双重风险 —— 不采用；"
                      "pyttsx3 仅开发期调试用",
        code_reuse="allowed",
    ),
    Decision(
        key="U22", title="VAD 人声检测选型", phase="P7",
        plan_quote="「开发 VAD 人声检测、实时收音、随时打断机制」（三、P7）——未指定实现",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="silero-vad（ONNX 可端侧，已被 sherpa-onnx 内置集成）；\n"
                   "webrtcvad 作为极轻量打断判定备选。",
        references=("https://github.com/snakers4/silero-vad", "https://github.com/wiseman/py-webrtcvad"),
        license_note="silero-vad MIT（其徽章图仍写 CC BY-NC 4.0，属历史遗留，以 LICENSE 原文为准）；"
                     "webrtcvad MIT",
        code_reuse="allowed",
    ),
    Decision(
        key="U23", title="【需求与平台冲突】iOS 无法后台常驻唤醒 — 已按平台分档", phase="P7",
        plan_quote="「集成后台常驻离线语音唤醒」（三、P7）——计划书**未按平台分档**",
        status=RESOLVED, source="人工授权", decided_at=_D,
        resolution="裁定：**仅 Android 承载后台唤醒，iOS 降级为前台唤醒**（两平台功能分档）。"
                   "Android 侧「后台常驻唤醒」仍须先做真机可行性验证"
                   "（Android 14 起麦克风类型前台服务启动受限、国内 ROM 后台清理策略），"
                   "再定权限与 UI 方案；iOS 只做前台唤醒。"
                   "该分档是对计划书 P7 原文的范围补充，由人工裁定，非 AI 自行决定。",
        references=("https://github.com/xfzen/xiaozhi-flutter-client",),
        code_reuse="n/a",
    ),
    # ---------------------------- P9 ---------------------------- #
    Decision(
        key="U28", title="增量微调框架与参数高效方式", phase="P9",
        plan_quote="「周期性增量微调流水线；支持微调 Jev 决策 Question 模板与垂类模型」（三、P9）"
                   "——未指定框架与微调方式",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="微调编排用 LLaMA-Factory；参数高效方式用 **LoRA**，显存不足时退到 4bit **QLoRA**；\n"
                   "底层张量适配库为 HuggingFace PEFT。"
                   "**训练作业本身不进 CI**：本机为 CPU 推理环境，无 GPU；\n"
                   "CI 只验证「数据集构建 → 配置生成 → 流水线编排」这段可确定的部分。",
        references=("https://pypi.org/project/llamafactory/",
                    "https://llamafactory.readthedocs.io/zh-cn/latest/getting_started/data_preparation.html",
                    "https://pypi.org/project/peft/"),
        license_note="LLaMA-Factory Apache-2.0；PEFT Apache-2.0",
        excluded_note="**github.com 与 raw.githubusercontent.com 在本机实测不可达**（ConnectTimeout），"
                      "故取证改用 PyPI 与官方 readthedocs —— 与 U11 对 github.com 的实测结论一致；"
                      "GitHub 仓库地址仅作为上游 canonical 记录，未在本机直接验证",
        code_reuse="allowed",
    ),
    Decision(
        key="U29", title="领域 SFT 数据集格式", phase="P9",
        plan_quote="「领域 SFT 数据集构建；剥离用户身份隐私；禁止把私有 Vault 原始隐私放入训练集；\n"
                   "公共 Workspace 知识库可用于训练」（三、P9）——未指定数据集格式",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="采用 LLaMA-Factory 的 **Alpaca 指令监督微调格式**："
                   "`instruction`（人类指令）/ `input`（人类输入，选填）/ "
                   "`output`（模型回答）/ `system`（系统提示词，选填）/ "
                   "`history`（多轮，选填）；\n"
                   "并在 `dataset_info.json` 里用 `columns` 做列名映射。",
        references=("https://llamafactory.readthedocs.io/zh-cn/latest/getting_started/data_preparation.html",),
        license_note="数据格式本身无许可约束；LLaMA-Factory Apache-2.0",
        code_reuse="allowed",
    ),
    Decision(
        key="U30", title="隐私脱敏实现", phase="P9",
        plan_quote="「原始用户隐私数据脱敏，禁止展示明文用户信息」「日志自动沉淀…隐私脱敏」"
                   "「剥离用户身份隐私」（三、P9）——未指定脱敏实现",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="Microsoft Presidio（analyzer + anonymizer）为主；\n"
                   "中文场景补充 cn-pii-anonymization，并保留一层自研正则（手机号 / 身份证 / 邮箱 / "
                   "账号标识 user-*）作为**确定性兜底** —— 中文姓名与地址的模型召回不稳，"
                   "纯模型方案会漏。",
        references=("https://pypi.org/project/presidio-analyzer/",
                    "https://pypi.org/project/presidio-anonymizer/",
                    "https://pypi.org/project/cn-pii-anonymization/"),
        license_note="Presidio MIT；cn-pii-anonymization 许可待逐项核对（引入前必须复核）",
        excluded_note="纯正则方案对中文姓名/地址召回不足，只作兜底不作主路径",
        code_reuse="allowed",
    ),
    Decision(
        key="U31", title="指标看板实现", phase="P9",
        plan_quote="「全维度指标监控看板搭建」（三、P9）——未指定看板技术",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="指标**由本项目 P9 模块计算**并以只读接口暴露（指标口径是资产，必须先固化）；\n"
                   "可视化层自托管 Metabase 消费该接口。Grafana 作为时序监控备选。",
        references=("https://www.metabase.com/", "https://grafana.com/"),
        license_note="Metabase 开源版 **AGPL-3.0**；Grafana **AGPL-3.0**",
        excluded_note="AGPL 属强著佐权：**仅内部自托管使用**；若将来对外分发或改造后再分发，"
                      "须重新评估许可义务。因此本项目只依赖自己算出的指标口径，不把看板代码并入产品",
        code_reuse="design_only",
    ),
    # ---------------------------- P10 ---------------------------- #
    Decision(
        key="U32", title="Agentic Loop 迭代上限与终止条件", phase="P10",
        plan_quote="「实现完整 Claude Code 风格 Agentic Loop 主循环」（三、P10）——"
                   "未定义循环多少轮、何时停",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="借鉴 OpenHands 把 **max iteration 显式化**的做法（并让模型知道上限）："
                   "本项目 `max_iterations = 8`；**连续 2 轮无实质进展即提前终止**；\n"
                   "到达上限仍未收敛则归档为「未收敛」并转人工，**不允许无限自转**。"
                   "与业务链路的「全局最大回退 2 次」同属有界循环原则。",
        references=("https://pypi.org/project/openhands-ai/",
                    "https://pypi.org/project/swebench/"),
        license_note="仅借鉴**设计思路**，不引入其代码；OpenHands MIT（备查）",
        code_reuse="design_only",
    ),
    Decision(
        key="U33", title="Hook 风险分级档位与审批口径", phase="P10",
        plan_quote="「所有生产变更风险分级，高危操作强制人工审批」+ 表 10「Hook 安全校验："
                   "禁止移除 user_id 行级过滤与鉴权；禁止读取未脱敏私有 Vault；\n"
                   "修改 Jev 模板、Wiki 公共知识库大规模变更强制人工审批」"
                   "（三、P10）——表注给了三条红线，**未给出分级刻度**",
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="依计划书表 10 的原文措辞，把三条红线落成三档："
                   "`low`（普通缺陷修复、测试补充，自动放行）；\n"
                   "`medium`（影响面较大的代码变更、知识库小范围维护，记录并复检）；\n"
                   "`high`（**命中红线**：改动行级过滤/鉴权、读取未脱敏私有 Vault、"
                   "修改 Jev 模板、公共知识库大规模变更）→ **强制人工审批，不得自动放行**。"
                   "**原文并未给出分级刻度，三档刻度由本项推导而来**；\n"
                   "**红线一律判 high，不设「紧急通道」**。",
        references=(),
        code_reuse="n/a",
    ),
    Decision(
        key="U34", title="沙箱执行实现", phase="P10",
        plan_quote="「对接 AI Infra 隔离沙箱，支持代码修改、命令执行、单元测试」（三、P10）"
                   "——未指定沙箱形态",
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="计划书原文只说「对接 AI Infra 隔离沙箱」，未给形态；据此推导：" 
                   "沙箱**是 AI Infra 的能力**（P0-1 已定义 edge/internal/sandbox 三层网络），"
                   "P10 只以**端口**对接，不自建隔离机制。"
                   "本机验证用**进程级受限适配器**（白名单命令、超时、工作目录限定、"
                   "禁止网络）；容器级强隔离属部署环境能力。"
                   "**控制的是「谁来执行」，不是「假定安全」**——因此执行前仍必须过 Hook。",
        references=(),
        code_reuse="n/a",
    ),
    Decision(
        key="U35", title="MCP 接入实现", phase="P10",
        plan_quote="「基于 MCP+Skill 协议集成检索、Git、文件、终端工具」（三、P10）"
                   "——未指定 MCP 实现",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="用官方 `mcp` Python SDK（其已在技术栈内）。"
                   "工具以 **Skill 注册表**组织，每个 Skill 显式声明**作用域**；\n"
                   "「Agent-Wiki 维护 Skill」**只允许公共 Workspace**，"
                   "在注册层就拒绝任何带 user_id 的目标。",
        references=("https://pypi.org/project/mcp/", "https://modelcontextprotocol.io/"),
        license_note="mcp Python SDK MIT；协议规范由 modelcontextprotocol.io 公开发布",
        excluded_note="`spec.modelcontextprotocol.io` 在本机**实测不可达**（ConnectError），"
                      "故规范取证改用 modelcontextprotocol.io 主站",
        code_reuse="allowed",
    ),
    Decision(
        key="U36", title="上下文压缩与记忆摘要策略", phase="P10",
        plan_quote="「开发上下文压缩机制，对接数据中台记忆摘要；**摘要脱敏，不带原始用户隐私**」"
                   "（三、P10）——未指定压缩实现",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="压缩分两级：**结构化裁剪**（按相关性/新旧裁剪，默认生效）+ "
                   "**模型摘要**（端口注入，LLMLingua 可作为实现之一）。"
                   "**压缩不替代脱敏**：顺序固定为「先脱敏，再压缩」，"
                   "且压完还要再验一次——压缩会把分散的片段聚到一起，"
                   "原本单看不敏感的字段拼起来可能就构成身份。",
        references=("https://pypi.org/project/llmlingua/",),
        license_note="LLMLingua MIT（本机未安装；默认走结构化裁剪）",
        code_reuse="design_only",
    ),
    # ------------------- 第二阶段｜旅游模块专项开发 ------------------- #
    Decision(
        key="U37", title="旅游实时数据源（天气 / 景区 / 交通）", phase="V1",
        plan_quote="「公共数据：天气 API、景区公开信息、交通实时状态；统一清洗、来源标记、"
                   "去重；输出转为 Agent-Wiki 公共 Workspace 的 source 源页面」（五、第二阶段）"
                   "——**未指定任何具体数据源**",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="天气用**和风天气**（中文文档、国内直连；\n"
                   "OpenWeatherMap 作为国际备用）；景区/POI 用 **OpenStreetMap Overpass API**"
                   "（开放数据、可自建镜像）。二者都只出**公共** source 页面，"
                   "经 P2 既有采集器统一清洗、来源标记、去重。",
        references=("https://dev.qweather.com/", "https://overpass-api.de/"),
        license_note="和风天气开发者服务有自己的服务条款（非开源许可，属**商业 API**）；"
                     "OSM 数据 **ODbL**（需署名，衍生库同许可）",
        excluded_note="`openweathermap.org` 在本机**实测 ConnectTimeout**，"
                      "不作为主路径；高德/百度地图 API 属商业授权，未纳入",
        code_reuse="allowed",
    ),
    Decision(
        key="U38", title="行程编排与路线规划算法", phase="V1",
        plan_quote="「生成初步路线；内置校验逻辑，检查路线时间冲突、距离不合理等问题」"
                   "（五、第二阶段）——未指定规划算法",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="规模化路线优化以 **Google OR-Tools**（TSP/VRP）为目标算法；\n"
                   "本项目**先实现不依赖外部求解器的贪心最近邻 + 显式校验**，"
                   "把 OR-Tools 作为**端口注入**的升级路径。"
                   "理由：求解器是可选增强，而「时间冲突 / 距离不合理」的校验是**必须始终生效**的底线。",
        references=("https://pypi.org/project/ortools/", "https://pypi.org/project/networkx/"),
        license_note="OR-Tools Apache-2.0；networkx BSD-3-Clause（本机均未安装）",
        code_reuse="design_only",
    ),
    Decision(
        key="U39", title="垂类 Agent 通用脚手架形式", phase="V1",
        plan_quote="「点餐、售后等其他垂类**直接复制本模块结构**，替换业务逻辑、工具集即可"
                   "快速接入」「模块低耦合，代码结构标准化」（五、第二阶段）"
                   "——未规定脚手架怎么抽",
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="依原文「可复制、可替换」的要求推导：抽出一个 **VerticalAgent 基类**，"
                   "把旅行/点餐/售后**共有且不可省略**的四件事固化下来——"
                   "① 职责边界（✅必做 / ❌必回退）② 回退规则（仅超域可退，域内禁止退）"
                   "③ 参数解析与缺失询问 ④ 结果归档到私有 Vault。"
                   "各垂类只填业务逻辑与工具集。",
        references=(),
        code_reuse="n/a",
    ),
    Decision(
        key="U40", title="行程重规划触发条件", phase="V1",
        plan_quote="「持续监听天气、交通、景区状态变化；**Jev 高速决策判断是否达到重规划条件**；\n"
                   "判定为 true 才触发重新调整行程」（五、第二阶段）"
                   "——原文给了三类事件，**未给出判定条件**",
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="依原文点名的三类事件（天气变化、交通延误、景区临时闭园）推导出"
                   "**确定性前置条件**，再由 Jev 做最终判定："
                   "① 景区闭园 → 必然触发；② 天气达到影响出行的等级 → 触发；\n"
                   "③ 交通延误超过行程时间余量 → 触发。"
                   "**前置条件不成立时根本不调用大模型**——原文「条件成立才调用大模型重算路线」，"
                   "前置判据省下的调用量正是这条设计的意义所在。",
        references=(),
        code_reuse="n/a",
    ),
    Decision(
        key="U42", title="云端 HTTP 接口层与账号表形态", phase="P7",
        plan_quote="三、P7 步骤5「对接云端流式接口…请求带上会话 Token」；\n"
                   "步骤7「上报携带会话身份」——**计划书只描述了接口行为，"
                   "未定义接口清单、路径、账号表结构**",
        status=RESOLVED, source="文档推导", decided_at=_D,
        resolution="从移动端 ApiClient 已有的三个路径反推接口清单（它是既成契约）："
                   "POST /api/session/login、POST /api/chat/stream、POST /api/device/report。"
                   "① /api/session/login 加入 PUBLIC_PATHS —— 登录本身不可能携带会话，"
                   "是**唯一允许匿名的业务入口**；其余业务接口一律仍需合法会话（七、11）。"
                   "② 账号表：计划书只要求「加盐哈希、不存明文」，未定义存储形态；\n"
                   "当前实现为存储根下的 users.json，只存 bcrypt 哈希。"
                   "③ 设备上报的 user_id **以会话为准**，请求体里冒充他人一律 403。"
                   "④ **不做公开注册接口**：计划书 P7 只规定登录，未定义注册；\n"
                   "匿名注册等于让人随意往系统塞账号，缺口不能顺手开。账号由 "
                   "scripts/create_account.py 离线开户。"
                   "⑤ **/api/chat/stream 尚未实现** —— 它要把 SceneScheduler.run() 接成 SSE，"
                   "属未完成项，不得视为已交付。",
        references=(), code_reuse="n/a",
    ),
    Decision(
        key="U41", title="输入风控：虚假信息与编码混淆的处置口径", phase="P3",
        plan_quote="用户指令（2026-10-18）：「做好风控（不能被用户以虚假信息或者 "
                   "base 代码混淆）」——**计划书原文未定义这一项**，"
                   "表 6 只规定了五层路由，未涉及输入侧风险筛查",
        status=RESOLVED, source="开源借鉴", decided_at=_D,
        resolution="① **不新增路由层**：五层链路已由【八】冻结，"
                   "风控做成**前置闸门**（core/input_guard.py），判定三档 —— "
                   "ALLOW / TAINT / BLOCK。"
                   "② **编码混淆**：base64、十六进制、百分号、Unicode 转义逐层解码后**重新扫描**，"
                   "深度上限 3 层；解出越权指令即 BLOCK，"
                   "仅含编码而无越权指令则 TAINT（编码本身即可疑，不作为事实依据）。"
                   "③ **虚假信息**：**不判真假，只判可信度** —— 未给可核验证据的"
                   "「已确认 / 官方证实 / 绝对」等断言一律 TAINT，"
                   "由 P6 质检与 Agent-Wiki 证据链决定能否入库；\n"
                   "给出 URL / DOI / 文件等证据位的输入不受此标记。"
                   "④ **判定真假不是程序该做的事**，本条只回答「能否作为事实依据」。",
        references=("https://owasp.org/www-project-top-10-for-large-language-model-applications/",),
        license_note="参照 OWASP LLM01 Prompt Injection / LLM09 Misinformation 的**风险分档思路**，"
                     "不引入其代码；OWASP 材料 CC BY-SA 4.0（备查）",
        code_reuse="design_only",
    ),

    # ==================== U43 起：三层权限分级 / 检索路由 / 复检 / Harness 评测 ====================
    # 来源日期 2026-10-19（接管后新增，与 U01–U42 的 2026-10-16 区分）。
    Decision(
        key="U43", title="Agent 能力分级模型", phase="P4",
        plan_quote="「无跨模块越权调用」「所有子 Agent 只读全局状态、仅写入自有字段」（三、P4）；\n"
                   "「模块边界界定（复用规则）」（五、5）——只规定「不得越权」，未规定能力如何**分级**",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="借 Claude Enterprise 的权限分层与多 Agent 治理实践：把「能力」从布尔开关升为**四级**——"
                   "L0 只读（可检索、不可写）/ L1 写自有字段（现有 writes 语义）/ "
                   "L2 可发起副作用（调外部工具、下单、发消息）/ L3 高危（支付执行、删数据、改权限）。"
                   "落地在 core/permissions.py 的 CapabilityLevel，与 SubAgent.capabilities 并存："
                   "capabilities 声明**能拿到哪些工具**，CapabilityLevel 声明**拿到后能用到什么程度**。"
                   "垂类 Agent 继承现有约束：may_hold_payment 默认 False（规范 10.11）。",
        references=("https://blog.chibakuma.com/cong-claude-enterprise-de-quan-xian-she-ji-kan-duo-dai-li-xi-tong-de-zhi-li-tiao-zhan-shui-neng-guan-li-shui-bu-deng-yu-shui-neng-zuo-shi-mo",),
        license_note="该文为第三方对 Claude Enterprise 权限模型的**分析文章**，非 Anthropic 官方源码，"
                     "未声明 License —— 只借鉴其「身份归属 / 管理范畴 / 功能能力 / 数据边界」四层划分的思维，"
                     "不复制任何代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U44", title="数据密级分级", phase="P0-1",
        plan_quote="「手机号、位置、支付信息等隐私字段独立加密存储」（三、P0-1）；\n"
                   "「区分公共 Workspace 与用户私有 Vault」（一、项目概述）——"
                   "给出的是**公私二分**，未规定密级**分级**",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="在 public/vault 二分之上补**四级密级**（与公私域正交，不是替代关系）："
                   "L1 公开 / L2 内部 / L3 敏感（手机号、位置、账单）/ L4 机密（凭据、支付要素）。"
                   "判据沿用国内多 Agent 安全实践的「四级 + 加密要求」表："
                   "L2 起传输加密，L3 起存储加密，L4 端到端加密且强制访问审计。"
                   "密级与现有 public/vault 的关系：**作用域决定「谁能看见」，密级决定「看见后怎么处理」**。"
                   "离线进化链路仍受 P10 红线约束：L3 及以上不得进入脱敏前的训练集。",
        references=("https://devpress.csdn.net/amd/6a55d12e662f9a54cb8f292b.html",),
        license_note="CSDN 社区文章，未声明 License —— 只借鉴四级分类表与加密要求的分档思路，不复制代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U45", title="用户角色分级与审批分层", phase="P0-1",
        plan_quote="「会话管理中间件：会话 Token 生成、校验」（三、P0-1）；\n"
                   "「付款动作必须经人工确认」（三、P8）——只有**是否登录**与**是否人工确认**两点，"
                   "未规定角色分级，也未规定何种操作需何种级别的审批",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="① 角色四级：owner（账号主体）/ admin（可管本账号下 Agent 配置）/ "
                   "member（日常使用）/ auditor（只读，可看审计不可改）。"
                   "② 审批分三档（借 AI Agent 治理的「按副作用定审批边界」思路）："
                   "Tier0 免审批（低风险可逆：建任务、打标签）；\n"
                   "Tier1 单人确认（用户可见或改数据：付款、改画像——对应 P8 人工确认红线）；\n"
                   "Tier2 双人/强校验（高危：删数据、改权限、批量外传）。"
                   "③ 三条硬原则：**默认拒绝**、**最小权限**、**权限不可传递**"
                   "（Agent 不得以「被调用者权限」替调用方升级，必须各自独立校验）。",
        references=("https://www.chronic.digital/blog/ai-agent-governance-controls",
                    "https://zdnetinside.com/knowledge/how_should_organizations_govern_permissions_for_ai_agents_in_2026.php"),
        license_note="两篇均为行业分析文章，未声明 License —— 借鉴其 Tier0/1/2 分档与"
                     "「default deny / propose-then-apply」设计，不复制代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U46", title="检索路由器：何时走单路线、何时走多路线", phase="P2",
        plan_quote="「BM25 + 向量检索 + Rerank 三重混合检索服务」（三、P2）——"
                   "规定了**有什么检索手段**，未规定**何时用哪一条**；\n"
                   "用户补充需求：关键字特别明确的直接走规则路由不走大模型",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="新增 rag/routing.py 的 QueryRouter，把查询分三档，判据**先看证据不看直觉**："
                   "① **规则直查**（单路线）：查询命中精确标识（ID / 错误码 / 型号 / 时间范围 / 枚举字段）"
                   "→ 直接查结构化库与 BM25，**完全不经过大模型**；\n"
                   "② **混合检索**（多路线）：判据为「无精确标识但语义可判」"
                   "→ BM25 与向量并行 → RRF 融合 → topN 候选 → Rerank → topK；\n"
                   "③ **大模型分析**：判据为「多意图冲突 / 证据互相矛盾 / 置信低于阈值」"
                   "→ 交给大模型，并**把该次歧义样本落盘**供 P9 增量微调与 Agent-Wiki Question 沉淀。"
                   "三档的选择**复用五层意图路由的结论**（P3 已冻结，不得新增第六层），"
                   "本路由器只在**检索侧**做细粒度决策，不改动意图链路。",
        references=("https://www.wickedsmartdata.com/articles/query-routing-in-rag-how-to-direct-questions-to-the-right-data-source-or-retrieval-strategy",
                    "https://blogs.oracle.com/developers/how-to-evaluate-production-rag-keyword-vector-sql-and-hybrid-retrieval"),
        license_note="前者为行业博客、后者为 Oracle 官方开发者博客，均非可复制代码仓库 —— "
                     "借鉴 rule-based vs LLM routing 的取舍判据与「按查询类型选路线」的评测口径，不复制代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U47", title="回答前的复检回路与逃逸上限", phase="P2",
        plan_quote="「内容质检 Agent｜全局防错、防幻、防假」（三、P6）；\n"
                   "「质检 Agent 校验纠错（调用 Wiki 做事实溯源）」（四、1）——"
                   "质检定位在**产出之后**，未规定**回答发出之前**是否要再检索一遍",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="新增回答前复检（rag/verification.py），三级评分照 Self-Correcting RAG 的 critic 链："
                   "① 检索相关性 → ② 答案 groundedness（每句须有 span 支撑）→ ③ 答案有用性。"
                   "关键三条借鉴结论：**失败时改写查询而不是用同一 query 重试**"
                   "（同 query 同索引必然召回同一批坏结果）；**必须有硬逃逸上限**（max_iterations=2），"
                   "否则对抗性查询会跑到 token 预算耗尽；**允许并必须实现 abstain**"
                   "（证据不足就说不知道，比编一个自信的错误答案便宜）。"
                   "复检不通过时降级为 abstain 并给出缺失证据清单，交 P6 与 Agent-Wiki 做 Question 沉淀。",
        references=("https://www.activewizards.com/blog/self-correcting-rag-pipeline-critic-agent-langgraph",
                    "https://engineering.zooz.com/@SangeethaJannapu/rag-but-make-it-reliable-verification-loops-that-actually-help-66fb279ec722"),
        license_note="两篇均为工程博客，未声明 License —— 借鉴三级评分、改写而非重试、"
                     "max-iteration 逃逸三项设计结论，不复制代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U48", title="Agent 独立运行入口形态", phase="P4",
        plan_quote="「逐个开发、逐个单测、逐个上线」（三、P4）；\n"
                   "「数据中台｜全系统唯一可信数据源」（三、P1）——"
                   "要求逐个可测可上线，但未规定**脱离会话**的独立调用入口形态",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="每个阶段 Agent 除「被 P3 调度」外，另提供**独立入口**："
                   "同一份 run() 逻辑，外层套 standalone 调用面（CLI + Python API 两种），"
                   "显式传 user_id 与权限上下文，**不经过 LangGraph 会话态**。"
                   "独立入口**不得绕过治理**：入库仍走 data_platform.governance 判定，"
                   "低置信照旧进 review 队列而不是直接写库 —— "
                   "「可独立跑动」不等于「可绕过风控」。"
                   "此形态对应 Agent Data Contract 的「每个 Agent 一份输入/输出/留存规约」。",
        references=("https://agenticaiguide.ai/ch_7/sec_7-2.html",),
        license_note="在线教程站点，未声明 License —— 借鉴「每个 Agent 一页规约（可读/可写/留存/脱敏）」"
                     "的契约化思路，不复制代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U49", title="Harness 自进化闭环的评测指标", phase="P10",
        plan_quote="「评测迭代效果，未达标持续循环修复」（四、2）；\n"
                   "「自动化评测脚本：计划书点名的 8 项指标」（三、P0-2）——"
                   "8 项指标是**业务链路**的，未定义**进化链路自身**如何被评测",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="Harness 自身评测分两组，与业务 8 项指标**分开计量**（双链路隔离，不得混算）："
                   "① **进化有效性**：缺陷修复成功率、回归引入率（修 A 坏 B）、"
                   "收敛轮次（达到目标所需 loop 数）、人工驳回率；\n"
                   "② **进化安全性**：红线触碰次数（harness/risk.detect_redlines）、"
                   "沙箱逃逸尝试、私有数据流入离线链路的次数（**必须为 0**）。"
                   "评测结果**回流驱动下一轮进化**：未达标则继续循环，"
                   "连续 N 轮无进展则停止并升级为人工处理（避免空转烧 token）。",
        references=("https://tunicyberlabs.com/en/blog/agentic-rag-2026-architecture-patterns-enterprise-knowledge-bases-2",),
        license_note="行业架构文章，未声明 License —— 借鉴「各层独立可测、评测门禁进 CI」的思路，"
                     "不复制代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U50", title="歧义样本的学习回流口径", phase="P9",
        plan_quote="「指标评估与增量微调体系」（三、P9）；\n"
                   "「日志沉淀与案例分类、领域 SFT 数据集」——"
                   "规定了要有 SFT 数据集，未规定**歧义/冲突样本**如何采集与回流",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="检索路由第③档（大模型分析）与复检不通过的样本，统一落盘为**歧义样本**："
                   "含原查询、召回证据、冲突点、最终判定、人工/系统裁决结果。"
                   "回流去向有三，且**必须先脱敏**（P10 红线）："
                   "① P9 增量微调的 SFT 数据集；② Agent-Wiki 的 Question 开放问题与 Contradiction 矛盾条目；\n"
                   "③ Jev 决策的难例集（用于 U03 阈值重新标定）。"
                   "**不进**离线链路原始日志——只进脱敏后的结构化样本。",
        references=("https://mem0.ai/blog/reducing-hallucinations-llms-with-grounded-memory",),
        license_note="厂商博客，未声明 License —— 借鉴「验证不通过即产生可学习信号」的闭环思路，"
                     "不复制代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U51", title="三层权限的合成规则", phase="P0-1",
        plan_quote="（同 U43/U44/U45）三层各自已定，但未定义**冲突时以谁为准**",
        status=RESOLVED, source="开源借鉴", decided_at=_D2,
        resolution="三层取**最严者**（deny overrides），不做加权、不做投票："
                   "Agent 能力级 / 数据密级 / 用户角色级三者任一拒绝即拒绝。"
                   "理由：权限决策里「严格」的代价是麻烦，「宽松」的代价是泄露，二者不对称。"
                   "另设**权限不可传递**硬约束：A Agent 调 B Agent 时，B 必须按**自身**权限重新校验，"
                   "不得继承 A 的权限（防止借道提权）。"
                   "该规则由 tests/test_permissions.py 以「造出越权看它红」的方式守护。",
        references=("https://zdnetinside.com/knowledge/how_should_organizations_govern_permissions_for_ai_agents_in_2026.php",),
        license_note="行业分析文章，未声明 License —— 借鉴 deny-overrides 与权限不可传递原则，不复制代码",
        code_reuse="design_only",
    ),
    Decision(
        key="U52", title="Agent 模块的命令行入口形态", phase="P4",
        plan_quote="三、P4「逐个开发、逐个单测、逐个上线」；U48 已裁定独立入口为函数接口，"
                   "但未定义**命令行壳**的形态与行为",
        status=RESOLVED, source="实现参数", decided_at=_D2,
        resolution="每个定义了具体 Agent 类的模块都加 `if __name__ == \"__main__\"` 入口，"
                   "委托 ai_butler/agents/cli.py。三条边界："
                   "①**不带参数只打印档案**，只读 ClassVar，不实例化、不连库、不碰外部依赖 —— "
                   "此前右键运行会静默退出（退出码 0、零输出），开发者以为文件坏了；\n"
                   "能跑但不说话比不能跑更糟，因为它伪装成正常；\n"
                   "②**执行业务必须显式 user_id 与 session_id**，缺一个即报错，绝不默认填假值；\n"
                   "③**不旁路任何校验**，权限三层、State 契约、写作用域全部复用 U48 的 "
                   "run_standalone，省掉的只有 LangGraph 编排。"
                   "另要求 sys.path 引导必须位于**业务 import 之前**：放在文件末尾的兜底 "
                   "救不了模块顶部的 `import ai_butler`。"
                   "由 tests/test_agent_cli.py 静态+实测双层守护。",
        references=(),
        license_note="本项目自定的实现参数，无外部来源可引用",
        code_reuse="n/a",
    ),
    # ==================== U53 起：运行时内核补齐（2026-10-23） ====================
    Decision(
        key="U53", title="工具统一调度的权限闸接线（在线编排路径）", phase="P3",
        plan_quote="三、P3「实现工具统一调度、重复调用拦截、无增量终止」；\n"
                   "七、验收 9–14「越权用例回归」——计划书要求工具统一调度与越权回归，"
                   "但未规定**三层权限判定落在调度路径的哪一步**",
        status=RESOLVED, source="实现参数", decided_at=_D3,
        resolution="闸门落在 `ToolDispatcher.dispatch` 内、handler 执行**之前**、"
                   "重复调用拦截**之前**（顺序即安全：放行一次不得靠缓存复用一生）。"
                   "① **未绑定权限上下文（ToolPermission）一律拒绝** —— "
                   "无从判定即拒绝，没有「未绑定时放行」的分支；\n"
                   "② 角色缺省 MEMBER（最保守的已登录身份）：会话链路尚未携带角色"
                   "（U42：/api/chat/stream 未实现），不给「大概是个 admin」留默认值；\n"
                   "③ 调度器自身按 L1 绑定：编排者不直接触发进程外动作，"
                   "需要 L2 及以上的调用方以自己的 AgentIdentity 求值"
                   "（dispatch(..., agent=...)），U51 权限不可传递；\n"
                   "④ Tier1/Tier2 在审批服务接入前**挂起不执行**（U45，计划书三、P8"
                   "「付款动作必须经人工确认」）—— 不等同于「先执行后补审批」；\n"
                   "⑤ 工具未声明 required 时按 L2（调外部工具）对待，"
                   "与 U43「不得用布尔量推导」同一立场。"
                   "此前权限只接在 run_standalone 与 CLI 上，dispatch 路径零求值 —— "
                   "属 MEMORY E-21/E-29/E-33「声明≠能力」的又一形态，本次接线的守护见 "
                   "tests/test_tool_permissions.py（每条断言对应一种具体越权手法）。",
        references=(),
        license_note="本项目自定的实现参数，无外部来源可引用",
        code_reuse="n/a",
    ),
    # ==================== U54：CI 平台切换到 GitHub Actions ====================
    Decision(
        key="U54", title="CI 平台由 Gitea + act_runner 切换为 GitHub Actions", phase="P0",
        plan_quote="三、P0-2「制定 Git 分支规范、CI 自动构建、自动部署流水线」——"
                   "计划书要求 CI 自动构建，但未指定托管平台；U11 曾据「github.com "
                   "在本机与工具链均不可达」裁定 Gitea + act_runner",
        status=RESOLVED, source="人工授权", decided_at=_D3,
        resolution="2026-10-23 用户指令「用GitHub不用gitea」，平台切换为 GitHub Actions。"
                   "**网络实测（切换依据，勿凭旧结论行事）**：github.com 主站与 git 端点"
                   "全域不通（直连/代理/SSH 22/ssh.github.com:443 均失败，"
                   "`git ls-remote` 报 Empty reply）——U11 的判定对此精确成立；\n"
                   "但 **api.github.com 可达（200）**、codeload 可达。"
                   "最终形态：① 代码由使用者在可访问 github 的终端**自行推送**"
                   "（本机 git 端点不通，无法代推）；② CI 完全跑在 GitHub 托管 runner 上，"
                   "**本机不需要任何 runner**——外部 action（actions/checkout@v4、"
                   "setup-python@v5、upload-artifact@v4、dart-lang/setup-dart@v1）与 "
                   "docker.io 服务镜像均可直接用；③ ci.yml 从 .gitea/workflows/ 迁至 "
                   ".github/workflows/，服务镜像从 daocloud 源改回 canonical 名；\n"
                   "④ 本机自建的 Gitea 容器与 remote 随之退场（数据卷保留待命）。"
                   "分支触发条件（main/develop/release/**/feature/**/hotfix/**）不变，"
                   "守护测试 tests/test_ci_pipeline.py、test_repo_hygiene.py、"
                   "test_devkit_scenario_library.py 的三处 CI_PATH 已同步。"
                   "U11 记录保留（历史裁定），本文档为其继任者。",
        references=(),
        license_note="平台切换由用户指令触发；网络可达性为本机实测事实，无外部来源可引用",
        code_reuse="n/a",
    ),
    # ==================== U55/U56：评测中台 Agent（2026-10-23） ====================
    Decision(
        key="U55", title="新增评测中台 Agent（结果+过程七大维度评测）", phase="P9",
        plan_quote="三、P9「全维度指标监控看板搭建；业务指标聚合统计」——"
                   "计划书要求指标监控，但未定义**过程侧**的评测口径；\n"
                   "用户指令（2026-10-23）「再补充添加一个评测中台 agent……"
                   "真正的评测绝非仅靠大模型离线评分，而是需要全面覆盖结果与过程的"
                   "七大核心维度」",
        status=RESOLVED, source="人工授权", decided_at=_D3,
        resolution="新增横切 Agent `eval_platform_agent`（评测中台 Agent，命名登记在 "
                   "core/naming.py 的 USER_AGENT_TITLES——来源是用户指令而非计划书原文，"
                   "故与计划书表分开登记）。它消费所有阶段 Agent 的过程痕迹"
                   "（检索/工具/大模型/异常四类），按七大维度评定，仅写入自有字段 "
                   "~{eval_platform}~。七维度（前六管过程、第七个管结果）："
                   "① 检索召回率 ② 证据忠实度 ③ 工具调用真实性 ④ 稳定性与异常 "
                   "⑤ 成本与效率 ⑥ 安全与权限 ⑦ 输出质量。"
                   "与「离线跑分」的本质区别：黄金数据集+大模型评分只覆盖结果侧一维；\n"
                   "过程侧（漏检索/幻觉/虚构成功/不稳定/无从判责/越权）必须靠痕迹评。"
                   "**未接线项（如实登记，不假装已完成）**：痕迹的**生产端**"
                   "（各阶段 Agent 运行时埋点，把 ToolDispatcher 调用写进 State）"
                   "尚未接线；本 Agent 当前消费调用方显式提供的 traces。"
                   "实现在 ai_butler/evaluation/（trace/dimensions/platform）"
                   "与 ai_butler/agents/eval_platform.py，守护见 "
                   "tests/test_evaluation_platform.py。",
        references=(),
        license_note="用户指令触发，无外部来源可引用",
        code_reuse="n/a",
    ),
    Decision(
        key="U56", title="七大维度评测的聚合与阈值口径", phase="P9",
        plan_quote="三、P9「业务指标聚合统计」——计划书未规定过程维度如何聚合成"
                   "总体结论，也未给各维度阈值",
        status=RESOLVED, source="实现参数", decided_at=_D3,
        resolution="① **fail-closed**：维度适用但数据缺失（没痕迹/没金标准）="
                   "无从判定=不通过，「没有记录」不能当成「没有问题」；\n"
                   "② **门槛维度取最差一次运行**（召回/忠实/工具/安全/输出）："
                   "坏过一次就是坏过，不拿平均值粉饰；计量维度（成本）看聚合，"
                   "稳定性看方差；③ **不适用≠通过**：纯问答用例的工具真实性记"
                   "「不适用」并从聚合剔除，不送虚假满分稀释总分；\n"
                   "④ **安全一票否决**：安全维度不通过则总体不通过，无论其他维度"
                   "多好（与 U49 进化安全性同构——安全性是准入项不是扣分项）；\n"
                   "⑤ **分数看趋势，结论看红线**：overall_score 为适用维度等权均分"
                   "（跨版本比较用），verdict 由红线决定，两者不互相推导；\n"
                   "⑥ 默认阈值：召回率 1.0、忠实度 1.0（零容忍幻觉）、一致性 1.0、"
                   "相似度 0.6；成本不设预算（权衡非红线）；\n"
                   "⑦ 维度二默认确定性启发式裁判（字符二元组包含度≥0.5），"
                   "LLM 裁判为可注入端口，报告如实记录用了哪种（不冒充模型级判定）；\n"
                   "⑧ 报告出口必经 Redactor 并 assert_deep_clean（P9 脱敏纪律）；\n"
                   "⑨ 维度 key 契约冻结（dimensions.DIMENSION_KEYS），"
                   "改名等于破坏历史可比性（与 metrics.METRIC_KEYS 同纪律）。",
        references=(),
        license_note="本项目自定的实现参数，无外部来源可引用",
        code_reuse="n/a",
    ),
    Decision(
        key="U57",
        phase="P0-1",
        title="append-only 会话事件账本（轨迹 / 回放 / 恢复的地基）",
        plan_quote="（计划书全文无「事件日志 / 轨迹 / 回放 / 恢复」任何表述——"
                   "本项为文档未定义项，按「开源借鉴」处理）",
        status=RESOLVED,
        source="实现参数",
        decided_at=_D3,
        resolution="2026-10-23 用户指令「按优先级和稳定性全部处理」中的 P2 项。"
                   "按「开源借鉴」实现 Event Sourcing 模式："
                   "① 不存当前状态，存到达状态的全部事件，状态 = 事件的 fold；\n"
                   "② append-only——存储端口（JournalStore）只有 load/append，"
                   "没有 update/delete，改历史只能追加新事件；\n"
                   "③ 每条事件 seq 连续 + sha256 链式绑定，verify() 断裂即拒"
                   "（fail-closed，回放前必须先过校验）；\n"
                   "④ 先脱敏后入账（Redactor.redact_deep + assert_clean），"
                   "与 harness/context.py 的顺序纪律同构；\n"
                   "⑤ 崩溃恢复 = 新实例从 store 重载续写（seq 不重号）"
                   "+ 最后一个 checkpoint 起 replay；\n"
                   "⑥ 事件类型与 evaluation/trace.py 五类过程痕迹一一对应，"
                   "账本同时是 P9 评测中台的数据源；\n"
                   "⑦ 接线：scheduler 的 Trace 埋点经 journal_observer 桥接落账"
                   "（observe.Observer 加 @runtime_checkable），harness loop 的"
                   "StageRecord 与终局快照实时落账（try/finally 覆盖 4 处 return"
                   "+ 异常路径）；不传 journal 时行为与接线前完全一致"
                   "（没有账本时不冒充已持久化）。",
        references=(),
        license_note="计划书未定义项；设计模式为 Event Sourcing（公开领域模式，"
                     "Martin Fowler 2005），另参照 dsh（DeepSeek Harness）的"
                     "append-only 日志对照设计",
        code_reuse="n/a",
    ),
    Decision(
        key="U58",
        phase="P10",
        title="上下文分项计量与 shadowing 压缩（对 U36 的扩展）",
        plan_quote="（计划书三、P10 步骤7 只定义「上下文压缩机制」与「摘要脱敏」；\n"
                   "分项计量口径、影子让位语义、token 估算方式均为未定义项）",
        status=RESOLVED,
        source="实现参数",
        decided_at=_D3,
        resolution="2026-10-23 用户指令「按优先级和稳定性全部处理」中的 P3 项，"
                   "对 U36 的压缩机制做两项扩展："
                   "① **shadowing（只让位不删除）**：U36 原实现的 dropped 只是计数，"
                   "被压内容连同让位原因（超条数/超预算）以影子记录留存，"
                   "带脱敏后原文，可经账本或 restore_shadowed() 重建完整上下文；\n"
                   "② **分项计量（contextBreakdown）**：压缩结果按 source / scope / "
                   "相关度分带（high≥0.8 / mid≥0.4 / low）统计条数与 token，"
                   "输出压力值 pressure=占用/预算——单维度总数回答不了"
                   "「哪类内容挤占预算」。"
                   "③ **token 口径如实标注**：默认 CharHeuristicCounter（CJK≈1 token/字，"
                   "其余≈1 token/4 字符），estimated=True；精确计数为可注入端口"
                   "（TokenCounter 协议），接 tiktoken 类实现即生效，"
                   "本项目不内置该依赖（E-26 形状：不为不可核实上游加原生依赖）。"
                   "④ 摘要 token 并入分项计量，不另起口径。"
                   "⑤ 预算内至少保留一条：空上下文比超预算更糟。"
                   "⑥ 让位记录与分项经 EV_CONTEXT_SHADOW 事件落账（U57 账本），"
                   "harness loop.run(journal=) 已接线。",
        references=(),
        license_note="计划书未定义项；shadowing/contextBreakdown 为 dsh"
                     "（DeepSeek Harness）对照设计",
        code_reuse="n/a",
    ),
    Decision(
        key="U59",
        phase="P3",
        title="人工审批服务形态：一次性凭据模型",
        plan_quote="三、P8「付款动作必须经人工确认」；三、P10「人工审批后台审核」"
                   "（只规定必须确认，未定义确认的机制形态）",
        status=RESOLVED,
        source="实现参数",
        decided_at=_D3,
        resolution="2026-10-23 用户指令「按优先级和稳定性全部处理」中的 P4 项。"
                   "计划书只规定「必须经人工确认」，机制形态为未定义项，"
                   "按「开源借鉴」实现一次性凭据模型（core/approval.py）："
                   "① **一次性**：approve 不直接触发执行；dispatch 凭 approval_id "
                   "consume 后才放行，用一次即焚——防「放行一次、缓存复用一生」"
                   "（与 U53 权限闸先于重复拦截同一立场）；\n"
                   "② **签名绑定**：票与（工具名 + 规范化入参签名）绑定，"
                   "批准调用 A 执行不了调用 B；\n"
                   "③ **主体绑定**：票与（agent, role）绑定，换身份消费即拒"
                   "（U51 权限不可传递的延伸）；\n"
                   "④ **无人应答即拒**：TTL 到期未决自动作废（expired），"
                   "不存在永远有效的票；clock 为可注入端口，测试不睡真时间；\n"
                   "⑤ **状态机单向**：pending → approved/rejected/expired，"
                   "不可回退，重复决策一律拒（幂等不等于放任）；\n"
                   "⑥ 审批必须署名（decided_by 非空）——匿名放行不可审计。"
                   "dispatch 三条分支没有第四条：带票→验票放行；无票且服务在→"
                   "建票挂起（返回携带 approval_id）；服务未绑→维持 U45 一律不执行。",
        references=(),
        license_note="计划书未定义项；一次性凭据（single-use token）为公开领域模式",
        code_reuse="n/a",
    ),
    Decision(
        key="U60",
        phase="P3",
        title="工具执行超时：协作式 + 按错误码判责",
        plan_quote="（计划书未定义工具超时与异常归因；评测维度四「稳定性与异常」"
                   "要求区分超时是接口故障还是任务本身超时）",
        status=RESOLVED,
        source="实现参数",
        decided_at=_D3,
        resolution="2026-10-23 用户指令「按优先级和稳定性全部处理」中的 P4 项。"
                   "① **协作式超时**：ToolSpec.timeout_seconds 声明即经单线程执行，"
                   "future.result(timeout) 判超时。协作式的含义是**不假装杀死线程**"
                   "——超时只保证调用方不再等待，执行体可能仍在后台，"
                   "副作用可能延迟发生，ToolResult.note 如实标注（比谎称已终止诚实）；\n"
                   "不限时的工具**不开线程**，与接线前行为逐字节一致；\n"
                   "② **按错误码判责**：handler 异常显式带 .code（如 PAYMENT_TIMEOUT / "
                   "INTERFACE_DOWN）即透传；超时为 TASK_TIMEOUT；未分类落 HANDLER_ERROR。"
                   "ToolResult.error_code/timed_out 供评测维度四直接消费；\n"
                   "③ snapshot 增 error_codes 聚合分布与 approval_bound 字段"
                   "（审计要能分别看见「服务没接」与「被拦下」）。",
        references=(),
        license_note="计划书未定义项；协作式取消（cooperative cancellation）为"
                     "公开领域模式，另参照 dsh 超时按错误码判责的对照设计",
        code_reuse="n/a",
    ),
    Decision(
        key="U61",
        phase="P7",
        title="图片输入：计划书「双输入」之外新增的第三条输入",
        plan_quote="三、P7「双输入（语音 + 文字）」——**原文没有图片输入**",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D3,
        resolution="2026-10-24 用户指令「支持图文识别和输出」追加。"
                   "① 端云分层不变：端上只取图/压缩/上传与渲染，**识别一律在云端**；\n"
                   "② 取图用 image_picker：拍照走系统相机 Activity，**不需要 CAMERA 权限**，"
                   "避免在 Android 14+ 媒体权限上自造坑；端上先压到长边 1600 / q88 再上传；\n"
                   "③ 识别供应商按 VISION_PROVIDER_ORDER 择一可用者：ollama（本地视觉模型，"
                   "可查 /api/tags 判断模型是否已拉取）优先，OpenAI 兼容端点备选；\n"
                   "**无可用供应商时 fail-closed 返回 503 + 可执行原因**（例如 ollama pull xxx），"
                   "绝不返回一段编造的『看起来像结果』的文本；\n"
                   "④ 客观事实（尺寸/亮度/主色）与模型文本**分开返回**，不依赖模型、可交叉校验；\n"
                   "⑤ 默认模型 qwen2.5vl:3b：实测 moondream 对中文 OCR 输出乱码（见台账 note），"
                   "qwen2.5vl 对『订单编号/金额/签收人』三行识别全对。"
                   "接口：GET /api/vision/availability、POST /api/vision/recognize。",
        references=(),
        license_note="计划书未定义项；视觉推理走 Ollama（MIT）与 OpenAI 兼容协议，"
                     "图像处理用 Pillow（HPND）",
        code_reuse="n/a",
    ),
    Decision(
        key="U62",
        phase="P7",
        title="图文输出：文字 + 服务端渲染结果卡片（不引入生成式绘图）",
        plan_quote="（计划书未定义图文输出的形态；P7 步骤5 只要求「逐字文字渲染 + 同步 TTS」）",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D3,
        resolution="2026-10-24 与 U61 同批追加。**图文输出 = 文字 + 一张真实生成的 PNG 卡片**，"
                   "由服务端 Pillow 渲染：标题、识别正文（按像素宽度折行，中文逐字试排）、"
                   "客观事实脚注（尺寸/平均色/亮度）。"
                   "三条边界：① 卡片随识别结果一起在 blocks 里下发（type=image, purpose=card），"
                   "移动端按序渲染，**图文同源**，不做二次拼装；② 没有中文字体时**在卡片上标注**，"
                   "不默默输出方块图；③ 不接生成式绘图模型：输出内容必须**可追溯到识别结果**，"
                   "生成图会让「这段文字对应哪张图」失去确定性，与评测维度二（证据忠实度）冲突。",
        references=(),
        license_note="计划书未定义项；卡片渲染用 Pillow（HPND），中文字体用系统微软雅黑/黑体",
        code_reuse="n/a",
    ),
    Decision(
        key="U63",
        phase="P7",
        title="声纹识别：登录后按账号登记与校验（计划书未定义）",
        plan_quote="（计划书 P7 未提及声纹；仅要求「唤醒后先校验本地登录会话状态」）",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D3,
        resolution="2026-10-24 用户指令「登录后可以语音唤起且支持声纹识别」追加。\n"
                   "① 模型取 3D-Speaker CAM++（192 维中文说话人 embedding，约 27MB），"
                   "经 sherpa-onnx SpeakerEmbeddingExtractor 推理，**只在云端跑**；\n"
                   "② **只存 embedding，不存音频原件**：embedding 无法还原成可听语音，"
                   "是「能比对且不留声纹原件」的最小留存，落盘为 storage_root/voiceprints.json"
                   "（每账号最多 3 条，超过淘汰最旧 —— 声纹会随年龄/设备漂移）；\n"
                   "③ 判定阈值 0.35 偏严：宁可让本人重说一次，也不要放错人（放错人不可回滚）；\n"
                   "④ **取不到分数一律判不匹配**，绝不把 None 当通过 —— 声纹是身份凭据，"
                   "任何「拿不到就放行」的实现等于没有声纹；\n"
                   "⑤ 只做**账号内 1:1**（比对自己的登记样本，取样本余弦的最大值）。"
                   "跨账号 1:N 不在本阶段范围：一旦阈值偏松就是大规模冒认；\n"
                   "⑥ 门槛：属成员能力（U64），访客不得登记 —— 否则匿名方能往声纹库写样本。"
                   "接口：POST /api/voice/speaker/enroll、POST /api/voice/speaker/verify。",
        references=("https://github.com/modelscope/3D-Speaker",),
        license_note="3D-Speaker (Apache-2.0)：只使用其发布的 CAM++ 模型权重（数据/权重），"
                     "不复用其训练与推理代码；实际推理走 sherpa-onnx",
        code_reuse="n/a",
    ),
    Decision(
        key="U64",
        phase="P7",
        title="登录模式改造：未登录只能文字查询，登录后才有语音与声纹",
        plan_quote="七、验收标准 11「未登录状态下所有业务接口拒绝执行」；"
                   "三、P7 触发时机「唤醒后先校验本地登录会话状态，未登录引导登录」",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D3,
        resolution="2026-10-24 用户指令「未登录时两端都只能文字查询，登录后可以语音唤起"
                   "且支持声纹识别」。这与验收标准 11 的字面存在张力，处理方式是"
                   "**不放宽鉴权，只放宽能力面**：\n"
                   "① 验收标准 11 约束的是**身份**（不得访问私有数据与业务副作用），"
                   "该条原样保留：私有 Vault 检索、设备上报、支付、记忆写入对访客一律拒绝；\n"
                   "② 新增**访客层**：`POST /api/session/guest` 签发**有期限（1h）+ 有额度（30 次）**"
                   "的访客凭据，走同一套 SessionAuthMiddleware 校验 —— **它不是免鉴权白名单**，"
                   "白名单里只放「签发凭据」这个动作；\n"
                   "③ 能力表集中在 core/access.py（AccessTier × Capability），所有入口查表，"
                   "未登记的能力直接报错而不是默认放行；\n"
                   "④ 语音输入/播报/唤醒/声纹/图文识别/私有检索一律要求成员层级。"
                   "理由不是省算力：**声纹与唤醒本身就是身份凭据**；\n"
                   "⑤ 层级从会话身份派生（guest: 前缀），**不采信请求体** —— 否则客户端改个字段"
                   "就自授权；\n"
                   "⑥ 端侧只拿能力档案**渲染按钮**，权限真值源在服务端；端侧表与之不一致时"
                   "以服务端 403 为准；\n"
                   "⑦ 登录成功后丢弃访客身份：两个身份并存会让「用哪个 token」变成猜的。",
        references=(),
        license_note="计划书未定义项：访客层级为人工授权新增，无外部来源",
        code_reuse="n/a",
    ),
    Decision(
        key="U65",
        phase="P7",
        title="语音模型的落地方式：VAD 端侧随包，ASR/TTS 走云端",
        plan_quote="三、P7 步骤4「开发 VAD 人声检测、实时收音、随时打断机制」；"
                   "一、项目概述「移动端（仅感知 + 交互）… 云端（全部智能运算）」；"
                   "U20 流式 ASR / U21「云端为主，端侧 VITS 兜底」",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D3,
        resolution="2026-10-24 用户指令「完善 ASR、VAD、TTS」后确定三者各在哪一端跑。"
                   "判据是**响应时间**与**安装包体积**，不是「哪个更先进」：\n"
                   "① **VAD 必须在端侧**（silero_vad.onnx 1.7MB，随包）："
                   "「随时打断」按毫秒算，走云端要一次网络往返，播报会继续说 —— 那叫排队不叫打断；\n"
                   "② **ASR 走云端**（流式 ZipFormer zh-14M int8，约 24MB）："
                   "识别属「智能运算」，放云端符合端云分层；端侧只负责录音与上传 16k 单声道 PCM16 WAV，"
                   "静音由云端 VAD 裁掉；端侧模型**可选**，缺模型时不得假装可用；\n"
                   "③ **TTS 走云端**（VITS-zh 多说话人，127MB）：随包会把 APK 从 49MB 推到 175MB，"
                   "不现实；端侧用 audioplayers 播返回的 WAV。U21 的「端侧兜底」保留为可选能力；\n"
                   "④ 模型**不进 git**：由 scripts/download_speech_models.py 按文件清单从镜像拉取，"
                   "缺模型时接口 fail-closed 返回 503 + 「跑哪个脚本」；\n"
                   "⑤ 端侧管线修复两个真实缺陷：采样从未被喂给 VAD/ASR（「随时打断」曾是死代码）、"
                   "TTS 失败会把整轮对话判成失败（现降级为只影响声音，不影响已渲染文字）。",
        references=("https://github.com/k2-fsa/sherpa-onnx",),
        license_note="以依赖形式使用：sherpa-onnx (Apache-2.0)、silero-vad (MIT)、"
                     "ZipFormer zh-14M 与 VITS-zh 权重 (Apache-2.0)、audioplayers (MIT)；"
                     "不复制其代码到本仓库",
        code_reuse="n/a",
    ),
    Decision(
        key="U66",
        phase="P3",
        title="检索侧的具体分类与工具选择（「用什么查」的显式化）",
        plan_quote="四、1「五层意图路由：规则匹配 → Jev → 向量 → 垂类决策模型 → 大模型兜底」；"
                   "三、P2「BM25 + 向量检索 + Rerank 三重混合检索服务」",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D3,
        resolution="2026-10-24 用户指令「检索信息和检索意图路由要么有具体分类和方法选择合适工具」。"
                   "落地为 retrieval/planner.py，**三层职责严格分开、互不越界**：\n"
                   "① 五层意图路由判「这句话想干什么」（冻结，不改层数）；"
                   "② 本模块判「这句话属于哪一类查询」，共 10 类：精确标识/事实问答/规则政策/"
                   "操作步骤/对比选择/推荐方案/私有回忆/闲聊/多意图/未知 —— **只收「会改变动作」的类别**；"
                   "③ 每类固定一条工具序列（primary → fallback → synthesize），工具注册表集中一处，"
                   "不在调用点写字符串；方法枚举含直查/BM25/向量/混合重排/私有库/业务工具/"
                   "直接回答/反问；\n"
                   "④ U46 的检索路线（直查/混合/大模型）**照旧消费，不改写**；两者冲突时记录信号而不偷改分类；\n"
                   "⑤ **工具按层级裁剪**（U64）：访客拿不到 vault.search 与业务工具，"
                   "被裁掉必须进 dropped_tools 并写明理由 —— 规划期就裁，别等执行期才 403，"
                   "否则用户看到的是「查不到」而不是「去登录」；\n"
                   "⑥ 判据先确定性后概率性：闲聊 → 多意图 → 逐条正则信号 → 五层兜底结论 → 未知，"
                   "每条结论都带 matched_signals，说得出是哪一条命中的（E-53）。",
        references=(),
        license_note="计划书未定义项：分类粒度与工具注册表为人工授权新增",
        code_reuse="n/a",
    ),
    Decision(
        key="U67",
        phase="P0-1",
        title="账号标识：手机号/邮箱二选一，PII 加密落盘",
        plan_quote="三、P0-1「账号密码加盐哈希存储」「隐私字段独立加密」；"
                   "未说明用手机号还是邮箱作账号，也未定义昵称与个人资料",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D4,
        resolution="2026-10-24 用户指令「登录注册（手机号或者邮箱记录账号）信息的长期记录」。\n"
                   "① **两种标识等价**：手机号（E.164 归一化，+86 默认）与邮箱（小写归一化）"
                   "都作为唯一登录标识，登记时二选一即可，**同一个账号只能绑定一个主标识**；\n"
                   "② **账号内部 ID 与标识分离**：对外只暴露 `user_id`（随机 opaque id），"
                   "手机号/邮箱永远不作为主键、不出现在 URL 与日志；\n"
                   "③ **PII 不明文落盘**：手机号与邮箱经 FieldCipher（AES-GCM）加密后存储，"
                   "另存 HMAC-SHA256 摘要列用于「查重 + 登录查找」——加密不可查，摘要可查，"
                   "两者分离才能既保证隐私又保证可用；\n"
                   "④ **展示一律脱敏**：接口返回 `138****8000` / `a***@example.com`；"
                   "**任何接口都不回传完整标识**，包括本人；\n"
                   "⑤ 密码沿用既有 bcrypt 加盐哈希（U42 / users.py），不引入第二套口令存储；\n"
                   "⑥ 登录失败计数与锁定：同一账号连续失败 5 次锁 15 分钟，"
                   "**锁定提示不区分「账号不存在」与「密码错」**，防账号枚举。",
        references=(),
        license_note="计划书未定义项：账号标识形态与 PII 加密策略为人工授权新增；"
                     "加密与摘要能力复用本仓库 core/security.py（FieldCipher / mask_*）",
        code_reuse="n/a",
    ),
    Decision(
        key="U68",
        phase="P0-1",
        title="忘记密码：一次性重置令牌 + 可插拔投递通道（开发通道生产拒绝）",
        plan_quote="三、P0-1 只规定「账号密码加盐哈希存储」，"
                   "**未定义找回/重置流程**，也未规定验证码如何下发",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D4,
        resolution="2026-10-24 用户指令「若密码不对可通过遗忘修改保存新密码」。\n"
                   "① **令牌而非验证码**：申请重置时服务端签发一次性令牌（32 字节随机），"
                   "**只存摘要**（SHA-256），TTL 15 分钟，**用后即焚**（状态机单向：待用 → 已用/已废）；\n"
                   "② **投递通道做成端口**（ResetChannel）：真实通道为短信/邮件；"
                   "本仓库当前**没有任何短信或邮件服务凭据**，因此内置 DevChannel "
                   "把令牌写入服务端日志并只在响应里带 `dev_token`——"
                   "**该通道在 production 环境启动即拒绝**（fail-closed），"
                   "绝不允许「开发方便」泄漏到线上；\n"
                   "③ **不可枚举**：无论账号是否存在，申请接口一律返回同一句「如该账号存在，已发送」，"
                   "且耗时对齐（不存在时也执行一次摘要计算）；\n"
                   "④ **重置即全端下线**：改密成功后吊销该账号所有既有会话（token 版本号 +1），"
                   "避免「改了密码但旧设备还登着」；\n"
                   "⑤ 新密码仍过强度校验（≥8 位），且**不得与旧密码相同**（避免「重置了个寂寞」）。",
        references=(),
        license_note="计划书未定义项：重置流程与通道抽象为人工授权新增，无外部来源",
        code_reuse="n/a",
    ),
    Decision(
        key="U69",
        phase="P0-1",
        title="昵称：可自定义也可随机刷新，唯一性不做强约束",
        plan_quote="三、P0-1 未定义个人资料字段，更未定义昵称规则",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D4,
        resolution="2026-10-24 用户指令「昵称可自己命名也可随机刷新」。\n"
                   "① **不做全局唯一**：昵称是展示名，不是登录标识；强行唯一会让「小明」被抢注，"
                   "也让随机刷新大量撞名。唯一性由 user_id 保证，昵称允许重复；\n"
                   "② 自定义规则：长度 2–16 个**字素**（emoji 算 1 个），"
                   "禁止控制字符与零宽字符（防视觉混淆，与 input_guard 的编码混淆防线同源），"
                   "禁止纯空白；\n"
                   "③ 随机刷新：从「形容词 + 名词」词表确定性拼装，"
                   "**可注入随机源以便测试**；刷新有频控（每分钟 ≤3 次），防刷；\n"
                   "④ 昵称参与展示，因此**不得含账号标识**（手机号/邮箱片段会被拒），避免变相泄漏 PII；\n"
                   "⑤ 昵称变更留审计（时间 + 前后值），历史值保留在事件账本，供申诉排查。",
        references=(),
        license_note="计划书未定义项：昵称规则为人工授权新增；词表为本仓库自拟",
        code_reuse="n/a",
    ),
    Decision(
        key="U70",
        phase="P0-1",
        title="用户自选大模型与自带 API Key：密钥加密、归属隔离、未经允许不得盗用",
        plan_quote="三、P0-1「统一 LLM 网关」「统一模型接入层」；"
                   "一、项目概述「统一的 LLM 网关」——"
                   "未定义「用户可选模型」「用户自带密钥」这类多租户形态",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D4,
        resolution="2026-10-24 用户指令「可自由选择大模型（可自己输入 api-key 接入新大模型），"
                   "默认免费或限时限免，按个人信息匹配，且不会被未经允许盗用」。\n"
                   "① **模型目录分三档**：`free`（平台免费额度，默认）/ `trial`（限时限免，带到期时间）"
                   "/ `byok`（用户自带 key）。前两档由平台出账，每账号有独立额度；\n"
                   "② **默认按用户画像匹配**（偏好 + 用途 + 语言），匹配结果带理由，"
                   "**不是黑箱**：返回 `matched_by` 说明命中了哪一条；无画像时落 `free` 档默认模型；\n"
                   "③ **BYOK 密钥加密且不可回读**：用 FieldCipher 加密存储，"
                   "接口只返回掩码（`sk-****abcd`）；**任何接口都不回传明文**，包括本人；\n"
                   "④ **归属隔离是硬约束，不是提示词**：每次调用都要带 user_id，"
                   "网关侧校验「该模型绑定属于该 user_id」才允许出站；"
                   "**别人的绑定一律 403**，不做「借用」；\n"
                   "⑤ **额度与账务按账号计量**：免费/限免档消耗记在该账号名下，"
                   "超额返回明确错误码（不是静默降级到更差模型）；\n"
                   "⑥ BYOK 的 key **只在服务端解密后用于出站请求**，"
                   "日志与事件账本一律走 Redactor 脱敏（复用既有 log_safety）；\n"
                   "⑦ 删除模型 = 同时删绑定与密文，**不做软删残留**。",
        references=(),
        license_note="计划书未定义项：多租户模型接入与 BYOK 为人工授权新增，无外部来源",
        code_reuse="n/a",
    ),
    Decision(
        key="U71",
        phase="P7",
        title="定位精度与推荐口径：服务端只收粗化坐标，导航链接本地生成",
        plan_quote="三、P7「设备采集（定位、状态）」；"
                   "二、第二阶段「旅游模块专项开发」——"
                   "未定义定位精度、地图渲染与导航跳转的实现方式",
        status=RESOLVED,
        source="人工授权",
        decided_at=_D4,
        resolution="2026-10-24 用户指令「可实时查看当前位置的定位；界面按喜好或所在地推荐；"
                   "含地图功能，可实时查看信息；查询导航时可自己生成或自动跳转三大导航软件」。\n"
                   "① **精度分级**：精确坐标只允许**留在端侧**用于展示与就近排序；"
                   "上报服务端前按 `geo.privacy_grid()` 粗化（默认 3 位小数 ≈ 110m，可配），"
                   "**服务端不保存精确坐标**，只存粗化点与「是否允许上报」开关；\n"
                   "② **不做地图渲染**：本仓库**没有任何地图服务 API key**，"
                   "因此不伪造底图、不假装能反查地址。界面展示「坐标 + 精度 + 获取时间」，"
                   "并明确标注「未配置地图 key，仅显示坐标」；\n"
                   "③ **导航链接本地生成**（满足「自己生成」）：按高德/百度/腾讯三家公开的"
                   "URI scheme 拼接，**不需要任何 key**，端侧用 url_launcher 打开；"
                   "同时给 web 兜底链接（未装 App 时不至于点了没反应）；"
                   "起点用「我的位置」语义，由各家 App 自行取当前定位，**不上传我方坐标给第三方**；\n"
                   "④ **推荐可解释**：按「所在地优先 + 喜好标签命中」打分，"
                   "每条结果带 `reason`（命中哪个标签/距离档），不做黑箱排序；\n"
                   "⑤ 关闭定位后**推荐降级为「按喜好」而非报错**，"
                   "并在返回里标注 `located=false`，让用户知道为什么顺序变了。",
        references=(),
        license_note="计划书未定义项：定位精度、地图与导航实现方式为人工授权新增；"
                     "三家导航 scheme 为公开协议，仅拼接 URL，不引入其 SDK",
        code_reuse="n/a",
    ),
    # ---- U72：重置令牌的邮件通道 ----
    Decision(
        key="U72",
        phase="P0-1",
        title="找回密码的真实投递：SMTP 通道（标准库实现，不绑定服务商）",
        plan_quote="三、P0-1「账号密码加盐哈希存储」「隐私字段独立加密」——"
                   "计划书定义了账号安全要求，未定义「忘记密码」的投递方式与通道选型",
        status=RESOLVED,
        source="实现参数",
        decided_at=_D4,
        resolution="U68 当时只留了端口，`smtp` 直接抛 `channel_not_configured` —— "
                   "等于「邮箱注册的账号在生产环境无法自助找回」。本次把通道补成真实现：\n"
                   "① **只用标准库 smtplib**，不引第三方 SDK：发一封重置邮件不该把项目"
                   "绑到某家邮件服务商；SSL/STARTTLS 由 `SMTP_SSL` 开关区分（465 / 587）；\n"
                   "② **收件地址走 `destination` 参数**，与日志用的 `masked` 分开 —— "
                   "通道必须知道真地址才发得出去，但**不许**把它写进日志；\n"
                   "③ **投递失败不抛给调用方**：抛了的话「账号存在 + 投递失败」与"
                   "「账号不存在」的响应就不一样，等于把不可枚举撕开一个口子；"
                   "失败记进 `failures` 供运维排查；\n"
                   "④ **手机号账号不硬发**：没有收件地址就记一条 skip，不静默假装发过 —— "
                   "这也是当前的真实缺口（见下方限制）；\n"
                   "⑤ 缺 `host/port/sender` 时启动即拒（fail-closed），并**报出缺的是哪个键**。\n"
                   "**已知限制（如实登记）**：手机号注册的账号没有邮箱，SMTP 救不了它；"
                   "要覆盖这部分用户需要短信通道（阿里云/腾讯云短信，需账号、签名与模板审核），"
                   "本仓库不接第三方短信 SDK，等凭据到位再按同一端口实现。",
        references=(),
        license_note="计划书未定义项；`smtplib` / `base64` 为 Python 标准库，无第三方代码复用",
        code_reuse="n/a",
    ),
    # ---- U73：多轮上下文的会话标识与隔离 ----
    Decision(
        key="U73",
        phase="P3",
        title="多轮上下文的会话标识：服务端签发 + 归属校验，不信任客户端自报",
        plan_quote="三、P3「会话上下文管理：多轮对话状态保持、上下文窗口控制、摘要压缩」"
                   " / 三、P4「禁止读取其他 user_id 数据」——"
                   "计划书要求「多轮」，但未定义会话标识由谁签发、如何校验归属",
        status=RESOLVED,
        source="实现参数",
        decided_at=_D4,
        resolution="此前 `scheduler/context.py` 有完整实现（窗口/压缩/摘要/账号分片），"
                   "但**全项目没有一个调用点**，聊天主链路每轮只发 system+user（见 MEMORY E-77）。"
                   "本次把它接上，同时按「记忆隔离」的要求把会话标识钉死：\n"
                   "① **标识只由服务端签发**（`secrets.token_hex(16)`），端侧只有回传权。"
                   "端侧此前把 `deviceId` 当 `session_id` 传，等于一台设备上所有对话"
                   "共用同一分片并永久累积 —— 这正是「Session ID 传递错误」的现实版本；\n"
                   "② **索引表记归属**（`core/conversation_index.py`，Redis/内存双实现，TTL 7 天），"
                   "未签发过的标识一律不认，同时挡住「客户端每句话新建一个分片」的存储放大；\n"
                   "③ **三种情况一律重签**：从未签发 / 格式非法 / 归属不符。后两者对客户端"
                   "**不作区分**（都回同一句 `不属于当前账号`），否则等于提供探测他人会话是否存在的口子；\n"
                   "④ **fail-closed**：索引后端异常时判「不是你的」，绝不放行 —— 放行一次就是隔离失效一次；\n"
                   "⑤ **访客不建会话**：原话属 PII；且 `guest:` 前缀含冒号，本就不是合法目录名"
                   "（`FileStore._check_name` 会拒），不能靠「它在真实路径上恰好没触发」来兜；\n"
                   "⑥ **功能关闭时不建索引**：建了索引却不写历史，会留下一堆指向空会话的标识，"
                   "排障时被误读成「历史丢了」；\n"
                   "⑦ **不写半轮**：LLM 失败/回答被截断时不落盘（「有问无答」比没有更坏，"
                   "下一轮模型会把同一个问题再答一遍或自行脑补）；复检推翻初稿时，"
                   "落盘的是**用户真正看到的那一版**（`revise` 之后），否则下一轮会把幻觉当「自己说过的话」引用；\n"
                   "⑧ **指代扩展只作用于检索侧**（短句 + 指代词时拼上一轮用户问题），"
                   "**不改写 query**：改写会让「用户问了什么」与「检索了什么」对不上，复检与评测无从对齐；\n"
                   "⑨ **构造本身要被断言**：抽出 `build_chat_orchestrator()` 作为唯一装配点，"
                   "守护测试断言 `app.state.chat_orchestrator.context_manager is not None` —— "
                   "「类有测试、链路没人构造」是绿灯下的空功能，光测类测不出来。\n"
                   "**退化必须说出口**：没装配 / 没标识 / 访客 / 索引坏了，一律单轮，"
                   "但原因进 `notes`。静默退化会把「没接上」伪装成「新用户第一次提问」。",
        references=(),
        license_note="计划书未定义项；会话索引是通用会话管理模式的实现，"
                     "未复制第三方代码；`secrets` / `re` 为标准库",
        code_reuse="n/a",
    ),
    # ---- U74：五层意图路由的接线与标签集 ----
    Decision(
        key="U74",
        phase="P3",
        title="五层意图路由接线：标签集复用查询分类，只接有实现的三层",
        plan_quote="一、项目概述 / 三、P3「落地五层意图路由：硬规则 → Jev 决策 → 向量相似度 → "
                   "垂类专用决策模型 → 大模型兜底」/ 八、五层路由冻结（表 6）——"
                   "计划书规定了层与顺序，未给出标签清单，也未说明各层「有实现才进管道」",
        status=RESOLVED,
        source="文档推导",
        decided_at=_D5,
        resolution="计划书原文只冻结了「层与顺序」（表 6）与目标句「落地五层意图路由」，"
                   "**既未给出标签清单，也未规定各层是否必须全部装配**——这两处空缺即本项的推导范围。\n"
                   "事实前提：此前三个层类（HardRuleLayer / VectorLayer / JevLayer）**有实现、有测试，"
                   "但全项目没有一处构造它们**（`grep` 引用数为 0），于是「五层路由」在运行时"
                   "不存在，而所有测试都是绿的 —— E-77 的第二种形态。本次把它接上：\n"
                   "① **标签集 = `QueryCategory` 的 10 个取值**（解决【文档未定义项 #1】）："
                   "不另造一套意图标签。检索侧已有 10 类且每类对应不同工具序列，"
                   "路由结论因此能**直接**被 `planner.plan()` 消费，不需要"
                   "「路由标签 → 查询分类」的翻译表（多一张表就多一处漂移）；\n"
                   "② **只接有实现的层**：L1 硬规则 / L2 Jev / L3 向量。"
                   "L4 垂类模型全项目只有 `VerticalDecisionModel` 协议、没有任何实现，"
                   "且 U03 要求先在中文验证集上标定；L5 大模型兜底与 L2 是同一件事"
                   "（同网关、同 prompt 形态、同解析），接了两份账。**层数仍是五层**（表 6 冻结），"
                   "装配结果必须是 `LAYERS` 的子序列，由守护测试钉住；\n"
                   "③ **L1 从 `planner.CATEGORY_SIGNALS` 派生**，不另写一份正则（E-44："
                   "两处正则必漂移）；表序即优先级，与 `classify()` 逐字一致；\n"
                   "④ **路由只补空缺，不覆盖确定性结论**：`classify()` 先跑正则，"
                   "全部落空时才采用路由标签。反过来就是「用大概对换掉确定对」，"
                   "而且同一条查询会时而查 A 时而查 B；\n"
                   "⑤ **兜底四级**：L2 失败/低置信 → 退回 planner 的 UNKNOWN → "
                   "`retrieval.hybrid_rerank` → `model.direct`。任一层异常只降级不阻断；\n"
                   "⑥ **降级必须留痕**：冷启动、模型缺失、路由整体故障，原因都进 "
                   "`degradation_notes` → `plan.intent.notes` → `done.notes`。\n"
                   "**实测（62 句自建语料，云端 deepseek-chat 作 L2）**：现状纯正则 66.1%，"
                   "接入后 83.9%（core 92.0%），L2 调用率 8.1%；剩余 10 条错误里 **8 条来自 L1**"
                   "（「我的」抢占、逗号不在分句符、「然后」不在并列词、「怎么样」被排除），"
                   "另 2 条是模型要求澄清而该场景不需要——说明下一个瓶颈是 L1 而非模型。",
        references=(),
        license_note="计划书未定义项（标签清单）；标签集取自本仓库既有分类，"
                     "未引入外部标签体系（未复用 home-assistant/intents 的具体清单）",
        code_reuse="n/a",
    ),
    # ---- U75：影子模式的执行方式与判定确定性 ----
    Decision(
        key="U75",
        phase="P3",
        title="影子期必须旁路执行，判定节点必须固定温度",
        plan_quote="三、P3「Jev 支持影子灰度模式」/「全链路降级与回滚机制」——"
                   "计划书要求影子灰度，未定义影子期间是否阻塞主链路，"
                   "也未定义判定调用的采样参数",
        status=RESOLVED,
        source="实现参数",
        decided_at=_D5,
        resolution="两件事都是实测暴露的，不是设计偏好：\n"
                   "① **影子改旁路**：原实现把 `shadow_mode` 判在「决策拿到之后」，"
                   "模型仍被完整调用一次，而 `route()` 是 `await`、单层超时 5 秒。"
                   "于是「只记录不决策」在成本上等于「照常调用一次模型」，"
                   "影子期给每个请求白加一次模型延迟（实测云端 P50 2.3s）。"
                   "改为：影子态立即返回「不采信」，模型调用丢后台任务，"
                   "失败一律吞掉（影子不得影响被观测系统）；`drain()` 仅供测试与优雅关闭。\n"
                   "② **判定节点 temperature=0**：网关原先不下发该字段，各厂默认采样，"
                   "同一条句子两次可能给出不同标签与置信。实测同一批 62 句连跑两轮，"
                   "澄清数 16 → 20，说明不确定性真实存在。判定类调用必须可复现，"
                   "否则路由行为不可回归、评测无法比较；**生成类调用不受影响**"
                   "（`temperature` 未指定时不下发，沿用各厂默认）。\n"
                   "③ **弱模型才关澄清**：`jev_allow_clarify` 默认 **True**（尊重 JEV-003）；"
                   "本机 3B 实测 6 次澄清 6 次错，那种情况下才设 false。",
        references=(),
        license_note="计划书未定义项；均为实现参数，无外部代码复用",
        code_reuse="n/a",
    ),
    # ---- U76：评测缺陷单与处置路由 ----
    Decision(
        key="U76",
        phase="P9",
        title="评测报告的「不通过」要落成可处置的缺陷单，并预先规定谁有资格自动修",
        plan_quote="三、P9「全维度指标监控看板搭建；业务指标聚合统计」/「迭代评估报告自动输出」；"
                   "三、P10「代码迭代 Agent **分析问题**、生成优化方案 → Hook + 质检风险校验 → "
                   "沙箱执行代码修改与自测 → 评测迭代效果，未达标持续循环修复」——"
                   "计划书要求「分析问题并生成优化方案」，**未定义「问题」用什么结构表达**，"
                   "也未规定哪些问题可以由离线链路自动处置",
        status=RESOLVED,
        source="文档推导",
        decided_at=_D5,
        resolution="计划书把「报告」与「修复」分成两个阶段，但中间的**交接结构**是空缺。"
                   "此前评测中台只吐 `PlatformReport`（哪一维、多少分、证据是什么），"
                   "harness 那头 `harness_proposals` 零生产者——两边都在，中间没线。"
                   "本项补的就是这个结构：\n"
                   "① **缺陷单 = 缺陷类别 × 严重度 × 处置路由**。类别只收「处置方式不同」的，"
                   "分得再细但不改动作就没意义（与 QueryCategory 同一原则）；\n"
                   "② **处置路由三档**（`auto` / `approval` / `manual`），"
                   "且**在缺陷产生时就定下来**，不留到受理时再判——"
                   "判据和处置放在一处，才不会出现「这条以前能自动修、现在不能」的漂移；\n"
                   "③ **安全缺陷永远 `manual`，且整批阻断自动处置**：表 10 第一条明令"
                   "禁止移除行级过滤与鉴权；让离线链路自动改权限代码，等于让被评的缺陷"
                   "去改判据，改完测试是绿的、漏洞还在。在一个会越权泄露余额的系统上"
                   "优化检索召回率，是把力气花在错的地方；\n"
                   "④ **无痕迹产出单条 `no_data`，不产七条**：痕迹缺失是埋点没接，"
                   "报成七个维度的问题会把工单池瞬间塞满；\n"
                   "⑤ **「判不了」不得记成「判不过」**：用例没给金标准、只跑一次，"
                   "属评测输入不全。为此在维度层新增 `DimensionResult.details['inconclusive']` "
                   "结构化标记（`missing_relevant_ids` / `missing_evidence_texts` / "
                   "`missing_expected` / `insufficient_runs`），缺陷层据**字段**而不是"
                   "匹配中文文案来分流——文案一改判据就失效，而且中文匹配就是 E-44 那类漂移源；\n"
                   "⑥ **`defect_id` = 用例 + 维度，跨轮次稳定**：否则同一缺陷每轮换编号，"
                   "趋势看不成曲线，也分不清「修好了」还是「换了个编号」；\n"
                   "⑦ **出口必经 Redactor**：缺陷单会进看板与工单，与 P9「禁止展示明文"
                   "用户信息」同一纪律。\n"
                   "跨进程还原时**不信任输入里的 `blocked_by_security` 布尔值**，"
                   "一律从缺陷重新推导——信任一个可以随手改的标记，"
                   "等于让调用方决定「安全缺陷能不能自动修」。",
        references=(),
        license_note="计划书未定义项（缺陷的表达结构）；未复用 ISO/IEC 25010、"
                     "Google 缺陷分级等外部体系的具体条目，仅取其「严重度分层」的通用思路",
        code_reuse="n/a",
    ),
    # ---- U77：缺陷受理与「自我修改」的边界 ----
    Decision(
        key="U77",
        phase="P10",
        title="缺陷受理的四道闸，以及本轮「自我修改」只做到变更单",
        plan_quote="三、P10 离线链路「Hook + 质检风险校验 → 沙箱执行代码修改与自测 → "
                   "**评测迭代效果，未达标持续循环修复** → 达标后沉淀记忆、生成版本变更单 → "
                   "人工审批后台审核」；表 10「修改 Jev 模板、Wiki 公共知识库大规模变更"
                   "强制人工审批」——计划书规定了链路顺序，"
                   "**未定义「谁有资格把缺陷变成提案」以及自动改代码的边界**",
        status=RESOLVED,
        source="实现参数",
        decided_at=_D5,
        resolution="四道闸，任何一道缺失都会让闭环变成摆设：\n"
                   "① **只收 `auto` 类**：`manual` 直接拒收并写明理由。"
                   "拒收不是失败，是正确处置——把不可自动修的缺陷塞进自动流水线，"
                   "等于让流水线去改判据；\n"
                   "② **安全缺陷存在时整批不受理**（`auto_repairable()` 返回空）；\n"
                   "③ **命令只从登记表取，不接受拼接**：沙箱白名单（U34）管得住程序名，"
                   "管不住参数，所以第零道闸是「连拼接的机会都不给」。"
                   "受理时再校验一次程序名在 `ALLOWED_COMMANDS` 内；\n"
                   "④ **工单绝不自行宣布修好**：`Proposal.done` 恒为 False。"
                   "早先在 loop 里把「提案用尽」写成 done=True，什么都没做却报"
                   "「已达标，循环收敛」（E-80 同源的虚假成功），不重犯。\n"
                   "**验证命令的口径**：工单跑的是「该子系统的现有回归测试」，"
                   "**不是**「复现线上缺陷」。绿=测试没覆盖这个缺陷（需补用例），"
                   "红=已有用例失败（直接拿到定位点）。二者都真实可行动，"
                   "且不假装跑一次 pytest 就等于复现了线上缺陷——真正的复现要靠重放痕迹，"
                   "那是 P9 数据源的事。\n"
                   "**质检结论由缺陷单直接给出**（`qa_verdict_from_batch`）："
                   "计划书要求 Hook「联动质检 Agent」，而缺陷单本身就是质检结论；"
                   "再让另一个组件算一遍，只会得到两份可能不一致的结论。"
                   "安全缺陷 → `block`，其余 → `pass`。"
                   "没有这一步，`qa_link` 会因「未收到质检结论」把每张工单都判成"
                   "待审批，闭环第一步就走不动。\n"
                   "**补丁应用器**：本项落地时如实登记为「未实现」（`diff` 为空、"
                   "`diff_pending` 标注、`requires_human_patch` 恒 True）——"
                   "声称能自动改源码而实际没有，比不做更坏（声明≠能力）。"
                   "该缺口已由 **U78** 补上：`harness/patch.py` 负责机械层，"
                   "`harness/coder.py` 负责向大模型要补丁。"
                   "本项保留的边界不变：**受理 ≠ 修好**，修没修好由 U79 的复评判。",
        references=(),
        license_note="计划书未定义项；均为实现参数与边界裁定，无外部代码复用",
        code_reuse="n/a",
    ),
    # ---- U78：大模型写补丁的安全边界 ----
    Decision(
        key="U78",
        phase="P10",
        title="让大模型写补丁，但补丁能不能落地由机械层判、由 Hook 放行、由验证保命",
        plan_quote="三、P10 步骤1「搭建基础代码修改沙箱，支持代码修改、命令执行、单元测试」；"
                   "三、P10 离线链路「代码迭代 Agent 分析问题、生成优化方案 → Hook + 质检"
                   "风险校验 → **沙箱执行代码修改与自测**」；三、P10 触发时机"
                   "「7×24 小时后台离线自动运行」——计划书要求「沙箱执行代码修改」，"
                   "**未定义补丁以什么形式落地、改动范围如何界定、失败如何回滚**；"
                   "无人值守这一条又使回滚成为必需品而非加分项",
        status=RESOLVED,
        source="文档推导",
        decided_at=_D6,
        resolution="**计划书只写了「沙箱执行代码修改与自测」，未定义补丁以什么形式落地、"
                   "改动范围如何界定、失败如何回滚**，这三处空缺即本项的推导范围。"
                   "**模型只产候选补丁，不产结论。** 三层分工，任何一层缺失都会让"
                   "「自动改代码」变成不可控：\n"
                   "① **创作层**（`harness/coder.py`）：向大模型要统一 diff。"
                   "给的是**磁盘上真实存在的源码片段（带行号）**，不是让它凭记忆写——"
                   "凭空写出的 diff 位置必然错，而错位的 diff 最难查（可能仍是合法 Python，"
                   "测试也过）。校验失败把**具体报错**回灌重试**一次**，仍不行就放弃："
                   "第二次失败说明它没理解任务，再试只是烧钱。\n"
                   "② **机械层**（`harness/patch.py`）：不含任何智能。解析（歧义即报错，"
                   "不做「尽力解析」）、范围校验、应用、回滚。\n"
                   "③ **时序**（`harness/loop.py`）：Hook 判风险 → 应用补丁 → 跑验证命令 → "
                   "**绿了 commit / 红了 rollback**。顺序不可调换：先应用再判 Hook 等于"
                   "让高危变更先落地；先跑命令再应用则验的是没改过的代码。"
                   "`finally` 里兜底回滚，覆盖 Hook 拦截、审批暂停、无进展、上限归档"
                   "以及决策层抛异常——「改了代码但循环异常退出」是留下半成品最容易的路径。\n"
                   "三条**结构性**防线（不依赖模型自觉）：\n"
                   "  - 范围白名单按缺陷类别收窄到子系统：修检索召回的补丁只能落在 "
                   "`ai_butler/retrieval/`；\n"
                   "  - `tests/` 与 `ai_butler/evaluation/` **全局禁改**。前者让「删断言」"
                   "变成「测试全绿」，后者是让被评的一方改判决书——表 10 第一条红线的同一逻辑；\n"
                   "  - 规模硬上限（3 文件 / 200 行）。超过说明这不是修复而是重写，重写该走人工；"
                   "「读不懂的自动变更」和「没审批」是一回事。\n"
                   "**三个如实登记的边界**：\n"
                   "  - `ok=True` 只表示「格式合法、范围合规、确有增删行」，"
                   "`CodeProposal` 里**没有也不会有**「已修复」字段；\n"
                   "  - **空补丁不算产出**：只有上下文行、没有 +/- 行的 diff 语法合法但什么都没改，"
                   "在创作层就拦下，不放它白占一次 Hook 与沙箱预算；\n"
                   "  - **模型主动弃权（`SKIP:`）是合法结局，不是失败**。实测中它因片段被截断"
                   "而拒绝写 diff，并明确指出「无法在不臆造上下文的前提下产出可被接受的统一 diff」"
                   "——这是这条链路里最该奖励的行为，必须与「写了但写错」分开记录。"
                   "据此把切片策略改为「小文件整篇给」（`FULL_FILE_CAP=700`）："
                   "切片省下的 token，抵不过「模型看不到关键分支」的代价。",
        references=(),
        license_note="统一 diff（unified diff）为通用格式，未复用任何具体项目的补丁应用实现；"
                     "自研解析与保守应用逻辑",
        code_reuse="n/a",
    ),
    # ---- U79：复评的两个判据与「不知道」的处置 ----
    Decision(
        key="U79",
        phase="P9",
        title="复评要分开判「有没有乱改」与「修好了没」，且不允许把「不知道」当成「通过」",
        plan_quote="三、P10 离线链路「沙箱执行代码修改与自测 → **评测迭代效果，未达标持续"
                   "循环修复** → 达标后沉淀记忆、生成版本变更单 → 人工审批后台审核」；"
                   "三、P9「评测中台对全链路做客观评价」——计划书把「评测迭代效果」写进"
                   "harness 的链路，**未定义「效果」由谁判、用什么判据，也未说明评价方与被评方"
                   "如何分离**",
        status=RESOLVED,
        source="文档推导",
        decided_at=_D6,
        resolution="**计划书把「评测迭代效果」写进 harness 的链路，但没写「效果」由谁判、"
                   "用什么判据，也没说评价方与被评方怎么分离**——这三处空缺即推导范围。\n"
                   "**评价的主体自始至终是评测中台，不是改代码的一方。** 这条边界一旦模糊，"
                   "闭环立刻变成自证（harness 改代码 → harness 说自己修好了 → 上线）。"
                   "故复评只接受两类证据：修复前的原报告（before）与本次重跑的新痕迹（after）；"
                   "harness 自己的 `converged` / `done` / `patch_applied` **一律不作为输入**——"
                   "那是「它认为」，不是「实际」。唯一从 harness 段取的是**它想改的 diff**，"
                   "而且只作为审计对象。\n"
                   "两个判据互不替代（混成一个总分两头都测不准）：\n"
                   "  - **没乱改**（`audit`，结构判据）：只看 diff，不跑代码。"
                   "越界路径 / 删断言 / 加跳过标记 / 移除保护（警告级）/ 规模失控。"
                   "**审计先于复跑**——乱改的补丁即使「效果变好」也不能接受，"
                   "因为它的效果可能来自「把断言删了」；\n"
                   "  - **修好了**（复跑，效果判据）：同一批用例重跑，看目标缺陷是否消失、"
                   "有没有原来通过的转成不通过（回归）、有没有新问题。\n"
                   "**结论是四态 + 一个「不知道」**：`accepted` / `rejected_audit` / "
                   "`rejected_regression` / `rejected_no_effect` / `unverified`。"
                   "没有布尔成功。`unverified` 的三种来源都**不得**升级为 `accepted`："
                   "没有 `TraceProducer`（无法重跑）、缺修复前基线（无从比较）、"
                   "全部用例复评报错。拿「补丁已应用」推断「修好了」，正是本项目反复避免的假成功。\n"
                   "**一个必须写进契约的坑**：补丁落在磁盘，而模块对象在进程里是已加载的缓存。"
                   "若复评在「应用补丁之前就已 import 被测模块」的进程里执行，它评的是旧代码，"
                   "会稳定报 `rejected_no_effect`——看着像「改了半天没用」，实际是评错了对象。"
                   "生产由重启解决；离线链路同进程时由 `TraceProducer` 负责重载，"
                   "没把握就抛异常（判 `unverified`），不要交出基于旧代码的「没修好」。"
                     "为此在 `rejected_no_effect` 的理由里直接写出这条排查提示。",
        references=(),
        license_note="计划书未定义项；取「结构判据 + 效果判据分离」与「fail-closed」的通用思路，"
                     "未复用具体评测框架的实现",
        code_reuse="n/a",
    ),
    # ---- U80：两个 Agent 的配合方式（装配点 + 固定顺序） ----
    Decision(
        key="U80",
        phase="P10",
        title="评测中台与 harness 的配合要有装配点与固定顺序，且「停在哪一段」本身就是结论",
        plan_quote="三、P10 离线链路「评测迭代效果，**未达标持续循环修复** → 达标后沉淀记忆、"
                   "生成版本变更单 → 人工审批后台审核」；三、P9「评测中台对全链路做客观评价」。"
                   "**计划书写了「循环修复」这个结果，未定义由谁按什么顺序驱动这两个单元、"
                   "修完的二次评测由谁发起、循环在什么条件下停**",
        status=RESOLVED,
        source="文档推导",
        decided_at=_D7,
        resolution="**计划书只写了「未达标持续循环修复」这个结果，没写这条循环由谁驱动、"
                   "按什么顺序、在哪停下**——这三处空缺即推导范围。\n"
                   "**一、先解决「装不上」，再谈「跑得对」。** U78/U79 之后，"
                   "`EvaluationPlatformAgent`、`HarnessAgent`、`StreamTraceProducer` 三件套"
                   "各自有实现、有测试，但全项目**没有一处把它们装在一起**："
                   "`ButlerAgent.enroll()` 只收带 `phase_code` 的阶段 Agent，评测中台是横切 Agent，"
                   "被直接拒收；生产端一次也没被构造过。闭环只在测试与一次性脚本里存在过——"
                   "即 E-77 的第四种形态（接口建好了，没有任何实现能进去）的第 N 次复发。"
                   "故设 `agents/repair_loop.py` 的 `build_repair_loop` 为**唯一装配点**，"
                   "`RepairLoop.run` 为**唯一驱动入口**，让「有没有插电」能像 "
                   "`app.build_chat_orchestrator` 那样被一条断言钉住。\n"
                   "**二、顺序是安全属性，不留参数。** 评 → 修 → 复评。反过来（先修再评）"
                   "等于让没有任何基线的东西自证有用；省略最后一段等于把「改了」当成「修好了」。\n"
                   "**三、五种停站必须各有其名，不许合并成「失败」**：\n"
                   "  - 没评（0 个用例）——缺陷数必然是 0，看着最干净，那叫没测；\n"
                   "  - 首轮全绿——`clean`，不是 `accepted`：闭环什么都没修；\n"
                   "  - 安全缺陷未解决——按 U76 整批阻断，**连 harness 都不调**；\n"
                   "  - harness 未运行（无沙箱）——与「跑了没改」是两件事；\n"
                   "  - 没产出可用补丁——没改动就没有「改动效果」可评，"
                   "硬跑一次复评只会把「没改」记成「白改」。\n"
                   "**四、真实管道接上后暴露的两个错。**（1）`revise` 是替换不是续写："
                   "真实管道弃答时会把用户真正看到的那一版单独发一条事件，"
                   "早先只认 `chunk`，接上后会拼出「初稿 + 弃答」这段谁也没说过的话，"
                   "用它算输出质量与证据忠实度两边都错。（2）补丁落盘后必须重建管道，"
                   "且重载顺序是**被依赖者在前**：`chat.py` 里 `from ... import plan as plan_query` "
                   "在 import 期就把名字绑进了自己的模块全局，只 reload planner 不够，"
                   "必须再 reload chat 它才重新 import；补丁若改在管道依赖上（如 "
                   "`scheduler/router.py`），依赖还要排在管道之前。漏一个不报错，只安静地评旧代码。\n"
                   "**三条如实登记的未做**：跨轮重试（**已由 U81 补上**——第二轮要在已提交补丁之上再改，"
                   "而撤销一个已提交的补丁本身是独立的高危能力，`PatchApplier.rollback` 在 `commit` 后失效"
                   "是刻意的）；首轮痕迹的来源（调用方给，线上日志或 U57 事件账本回放）；"
                   "排程（属运行时集成，本项只提供装配点与驱动）。",
        references=(),
        license_note="计划书未定义项；「编排层持顺序、Agent 只管自己那一段」为通用设计，"
                     "无外部代码复用",
        code_reuse="n/a",
    ),
    # ---- U81：跨轮重试的边界、判据与审批 ----
    Decision(
        key="U81",
        phase="P10",
        title="跨轮重试的四道约束：板块边界、错误信息指纹、撤销审批、逐轮溯源",
        plan_quote="三、P10 离线链路「…**未达标持续循环修复** → 达标后沉淀记忆、"
                   "生成版本变更单 → 人工审批后台审核，驳回终止迭代」；"
                   "用户指令（2026-10-25）：「不改动基本流程和保密的情况下，将功能进行实现，"
                   "并给出边界约束（保障修改的代码只负责自己的板块），跨轮重试要查验"
                   "错误信息已更改，若要撤销已提交补丁则需人工确认，每轮痕迹要有迹可循"
                   "能溯源知道问题出现在哪，改了什么，为什么要这样改」。"
                   "**计划书写了「持续循环修复」与「人工审批」两个结果，未定义循环的刹车条件、"
                   "跨轮时改动范围怎么收、以及「撤销一次已生效的变更」要办什么手续**",
        status=RESOLVED,
        source="文档推导",
        decided_at=_D8,
        resolution=(
            "**计划书只写了「未达标持续循环修复」与「人工审批后台审核」，没写循环凭什么停下、"
            "跨轮的改动边界、以及撤销已提交补丁是不是一次普通回滚**——这三处空缺即推导范围。\n"
            "**一、刹车条件不是「轮次用完」，是「错误信息有没有变」。**（`evaluation/fingerprint.py`）"
            "每轮复评后用归一化证据指纹比对前后两版缺陷：数字与时间戳归一（「仍不通过 3 个」与"
            "「2 个」是同一形态），路径与标识符保留（换文件、换分类结论是真变化）。一字未变即停"
            "（`no_progress`）——跑满 N 轮却从未推进，只是把同一动作重复 N 次，代价却是 N 次模型"
            "调用与 N 次全量评测。归一化方向刻意偏保守：把抖动当进展会让刹车永久失效，"
            "把微小进展当没进展只是提前转人工（可恢复）。\n"
            "**二、跨轮的边界分两层，且都不是「信任模型自觉」。**（`agents/repair_loop.py` 的 "
            "`RoundScope` + `harness/patch.py` 的 `frozen_paths`）第一层是板块：本轮可改的路径前缀"
            "＝本轮可自动处置缺陷的类别白名单**并集**（取交集会让两个板块的缺陷谁都改不了）。"
            "第二层是历史：前几轮已提交补丁动过的文件本轮**不可再改**——同一文件叠两份自动补丁，"
            "出问题时说不清是哪一轮造成的，而责任不可分割的自动化改动比改错更难处理。"
            "落地是两道：候选文件从模型上下文里剔除（看不到就不会改），产物再用 `PatchGuard."
            "frozen_hits` 判一次（模型凭记忆硬写也拦得住）。\n"
            "**三、撤销已提交补丁与验证失败回滚是两件事，必须两个入口。**（`harness/revision.py`）"
            "`PatchApplier.rollback` 只管「还没生效」，`RevisionLedger.undo` 管「生效过的变更」，"
            "后者要一张一次性审批票，签名绑定 round_id（批准撤 R1 的票撤不了 R2），"
            "撤销前逐文件验漂移（内容已不是本补丁的产物就拒，盲写回会冲掉之后的改动），"
            "且已撤销的轮次不得二次撤销。并成一个入口的后果是：前者的高频会让后者的门槛形同虚设。"
            "**只有到「改不下去」才申请票**——只要本轮还有别的板块可改，就不该来打扰人。\n"
            "**四、每轮留一条能自证的记录，不是操作日志。**（`harness/trail.py`）`RoundRecord` 六段："
            "缺陷（case+维度+证据+定位提示）→ 本轮边界（可改/锁定/冲突）→ 实际 diff 与命中文件 → "
            "模型给的理由（弃权时是它弃权的原话）→ 验证命令与 exit → 复评结论与逐条缺陷的去留，"
            "外加「本轮之后错误信息变了没」。缺任何一段，「溯源」就退化成翻聊天记录。"
            "账本只追加不改写：某轮后来被撤销也保留，另记撤销凭证——抹掉「改坏过」的那一版，"
            "等于把最有价值的教训一起删了。\n"
            "**五、顺带修正 U79 的一处口径。**复评原来的 `accepted` 只要求「至少一个用例转通过且"
            "无回归」，于是 3 条缺陷修好 1 条也算成功。新增 `partially_fixed` 态：它既不是"
            "`accepted`（用户要的是「成功解决问题」），也不是 `rejected_no_effect`（明明有效果）；"
            "把它记成任一态都是在训练自己说假话。"),
        references=(),
        license_note="计划书未定义项；「幂等 / 一次性凭据 / append-only 事件账」为通用模式，"
                     "复用本仓库既有的 `core/approval.py` 与 `core/event_journal.py` 的纪律，"
                     "未引入外部代码",
        code_reuse="n/a",
    ),
    # ---- U82：一次会话里跑第二遍时，账本凭什么还认得清 ----
    Decision(
        key="U82",
        phase="P10",
        title="跨次运行的轮次编号与账本唯一性（同一实例第二次 run 的四个坑）",
        plan_quote="三、P10 离线链路「…未达标持续循环修复 → 达标后沉淀记忆、生成版本变更单 → "
                   "人工审批后台审核」；用户指令（2026-10-25）「每轮痕迹要有迹可循能溯源"
                   "知道问题出现在哪，改了什么，为什么要这样改」。"
                   "**计划书只描述了「一轮一轮地修」，没有定义「同一次会话里第二次运行」的"
                   "轮次命名——而这正是「批准撤销 → 带回票重跑」的唯一用法**",
        status=RESOLVED,
        source="文档推导",
        decided_at=_D8,
        resolution=(
            "**计划书对这一层只字未提**：它写的是「未达标持续循环修复」与「人工审批后台审核」，"
            "即「一轮一轮地修」，没有写「同一次会话里的第二遍」——而带票重跑正是"
            "「人工审批」那一步之后唯一会发生的事。四处缺陷同源：**轮次编号的作用域被当成了"
            "「一次 run」，而账本的作用域是「整个会话」**。\n"
            "**触发点**：U81 的真机演示（`.tmp/round-retry-demo.py`）按设计要求跑了两遍——"
            "第一遍改坏了，第二遍带审批票撤销后重跑。跑完账本里出现两条 R1，且撤销凭证是空的。\n"
            "**一、轮次号跨次运行续接。**（`RepairLoop._next_round_no`）第二次 `run()` 接在账本里"
            "已有 `R<n>` 的最大值之后，不再从 R1 重新数。不续接的后果两处都是硬的：溯源账里两条"
            "不同的 R1，`get(\"R1\")` 只返回第一条，第二条成了文件里有、谁都读不到的影子账；"
            "版本账那边更直接——`RevisionLedger.record()` 拒绝同一轮二次提交，于是第二次运行"
            "**根本提交不了补丁**，报的还是「已登记过」这种看不出因果的错。缺口（R1、R3）不影响："
            "重号才是问题，跳号不是。\n"
            "**二、溯源账自己也要拒重号。**（`harness/trail.py` 的 `TrailError`）只靠调用方自觉"
            "续接是不够的：账本不拦，写进来的就是两根同名记录，而且失败发生在**读的时候**"
            "（`get()` 取第一条）——那是事后，人已经照着它做决定了。所以 `append` 查重、抛错、"
            "不落盘，与版本账同一判据。\n"
            "**三、早退分支必须带着撤销凭证。**（`run()` 的 `harness_not_run` / `no_patch` 两条"
            "返回 + `_ask_undo`）撤销生效后紧接着「本轮没补丁可评」就早退，是最常见的一条路"
            "（撤完本来就不该再改）。以前这两条 `_finish` 只带 `reason` 不带 `undone`，于是"
            "`outcome.undone` 为空：人看到文件已经变了，却查不到是谁撤的——比「没撤」更难查。\n"
            "**四、补丁段必须给改动面汇总。**（`agents/p10_harness.py` 的 `_written`）补丁段原来"
            "只有逐份明细 `results` 与撤销凭据 `committed`，而溯源账读的是汇总 `paths` / `touched`，"
            "于是真机账上写着「改动：True []（0 行）」，实际改了一个文件。**账报错数比缺字段"
            "更危险**：人正是照着它决定撤哪一轮。汇总只算 `ok=True` 的结果之并集（应用失败的那份"
            "已回滚，算进去会谎报改动面），且只在一处算——读账的地方各自从明细推导，迟早推出"
            "不同的数（E-44 的老毛病）。"),
        references=(),
        license_note="计划书未定义项；「编号全局唯一 + 账本拒绝重号」为通用记账纪律，"
                     "无外部代码复用",
        code_reuse="n/a",
    ),
)


def get(key: str) -> Decision:
    for d in DECISIONS:
        if d.key == key:
            return d
    raise KeyError(key)


def resolved() -> tuple[Decision, ...]:
    return tuple(d for d in DECISIONS if d.status == RESOLVED)


def pending() -> tuple[Decision, ...]:
    return tuple(d for d in DECISIONS if d.status == PENDING)
