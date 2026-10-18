# Agent-Wiki 知识库开发规范

> **计划书依据**：三、P0-1「输出三份配套规范：《Agent-Wiki 知识库开发规范》…」
> **定位**：本文档约束知识本体的字段、页面形态、双库隔离与编译产物。
> **产出阶段**：P0-1（存储与编译）／P2（检索接入）／P6（质检溯源）

---

## 1. 本体八要素

计划书原文：「本体包含：实体 Entity、来源 Source、声明 Claim、证据 Evidence、
关系 Relation、矛盾 Contradiction、开放问题 Question、综合摘要 Synthesis。」

| 要素 | 类型标识 | 专有必填字段 | 出处 |
|---|---|---|---|
| 实体 | `entity` | `name`、`entity_type` | 一 |
| 来源 | `source` | `url` | 三、P2「保存来源元数据」 |
| 声明 | `claim` | `claim_type`、`description`、`subject_id`、`object_id` | 三、P1 |
| 证据 | `evidence` | `source_id`、`source_text`、`text_unit_id` | 三、P1；U17 |
| 关系 | `relation` | `subject_id`、`predicate`、`object_id` | 一 |
| 矛盾 | `contradiction` | `left_id`、`right_id`、`reason` | 三、P1 |
| 开放问题 | `question` | `text` | 三、P6 |
| 综合摘要 | `synthesis` | `summary_of` | 三、P6；七、20 |

缺少专有必填字段 → 抛 `OntologyError`，**不得静默入库**。

---

## 2. 页面形态

人类可读 Markdown + YAML frontmatter + 固定小节（借鉴 Basic Memory 的范式，U18）：

```markdown
---
id: claim-0001
type: claim
title: 西湖春季最佳
status: TRUE
confidence: 0.93
valid_from: 2026-03-01
evidence_ids: [ev-1]
user_id: null          # 公共 Workspace 页面不得出现该字段
---

# 西湖春季最佳

## Observations
- 西湖春季最佳
## Relations
- relates_to: entity:西湖
```

**固定小节**：`## Observations` 与 `## Relations`。

**status 语义随类型而定**（不得混用）：

| 类型 | 允许取值 | 出处 |
|---|---|---|
| `claim` | `TRUE / FALSE / SUSPECTED / ARCHIVED` | U17（GraphRAG）；三、P2 |
| `source` | `active / archived` | 三、P2「过期来源标记为 status:archived」 |
| 其余要素 | 计划书未定义，留空 | — |

---

## 3. 双库隔离（硬边界）

计划书：「区分公共 Workspace（全用户共享公开知识）与用户私有 Vault（绑定 user_id）」

| 规则 | 内容 |
|---|---|
| 公共 Workspace 页面 | **不得**携带 `user_id`（计划书三、P1：素材溯源库无用户 ID） |
| 用户私有 Vault 页面 | **必须**携带 `user_id`，且必须等于目标账号 |
| 写入 | 作用域由 `user_id` 参数决定；页面自带 user_id 与目标作用域不符即拒绝 |
| 读取 | 只读指定作用域，**绝不跨账号** |
| 检索 | `list_pages_for_retrieval(user_id)` = 公共 Workspace + **本人** Vault，不遍历他人目录 |
| 编译 | 公共库与私有库各自独立编译，作用域目录不混 |

**实现方式**：公共库与私有库**共用同一套 schema**，靠作用域 + user_id 隔离，
不建两套结构（借鉴 cognee 的 NodeSet 分组，U19）——避免 schema 漂移。

---

## 4. 编译流水线

计划书：「提供编译流水线，输入 Markdown 页面输出机器可读缓存，
自动检测声明、证据、矛盾、过期素材」

**三份产物**（计划书一明确点名）：

| 产物 | 内容 |
|---|---|
| `pages.json` | 页面索引：id / type / title / status / confidence / user_id / invalid_at / contradicts |
| `claims.jsonl` | 每行一条声明，字段见第 5 节 |
| `agent-digest.json` | 注入 LangGraph State 的精简摘要，受 `wiki_max_digest_size` 约束 |

**四类报告**（写为 `report.json`）：

| 报告 | 触发条件 | 依据 |
|---|---|---|
| 矛盾 | 存在 `contradiction` 页面，或被 `contradicts` 指向 | 八 `wiki_auto_report_contradiction` |
| 低置信 | claim 置信 < 阈值 | 八 `wiki_auto_report_low_confidence` |
| 证据缺失 | claim 无 `evidence_ids` | 七、19 |
| 过期归档 | source 状态为 `archived` | 三、P2 |

**低置信阈值不新造数值**：沿用计划书【八】已有的 `jev_confidence_threshold = 0.82`。

**编译开关**：`wiki_compile_auto_run=false` 时只产出报告、不落缓存文件。

---

## 5. claims.jsonl 字段表

以 Microsoft GraphRAG 的 covariates 为起点（U17），叠加矛盾语义（U16）：

| 字段 | 说明 | 来源 |
|---|---|---|
| `claim_id` | 声明 id | U17 |
| `claim_type` | 声明类型 | U17 |
| `description` | 声明内容 | U17 |
| `subject_id` / `object_id` | 主客体 | U17 |
| `status` | TRUE / FALSE / SUSPECTED / ARCHIVED | U17 + 三、P2 |
| `confidence` | 置信度 0~1 | 三、P1 |
| `valid_from` / `valid_to` | ISO8601 有效期 | U17 |
| `invalid_at` | **失效时间**（矛盾时写这里，不删旧声明） | U16 |
| `contradicts` | 与之矛盾的声明 id 列表 | U16 |
| `evidence_ids` | 绑定的证据 id | 三、P1 |
| `user_id` | 公共页为 null，私有页为账号 | 三、P0-1 |

---

## 6. 矛盾语义（不删除，只失效）

借鉴 Graphiti 的 fact-invalidation（U16）：

- 发现矛盾时**不删除旧声明**；
- 给旧声明/关系写 `invalid_at`，新增 `contradicts` 关系；
- **保留完整时间线**，供质检 Agent 做事实溯源与人工复核。

**禁止**：直接删除或覆盖被推翻的声明——那会丢失溯源链，违反「证据链不丢失」要求。

---

## 7. wiki_digest 与 LangGraph State

计划书三、P3：State 必填字段包含 `wiki_digest`。

- 生成：按置信度**从高到低**保留声明，直至达到 `wiki_max_digest_size = 12000` 字节上限；
- 超限必须置 `truncated=true` 并给出 `claim_count`，**绝不静默丢内容**；
- digest 只放最小充分信息（claim_id / subject_id / object_id / status / confidence），
  正文留在页面里按需检索。

---

## 8. 检索规则

计划书三、P2：「检索入参携带 user_id；合并返回公共 Workspace + 当前用户私有 Vault 内容；
不会加载其他用户私有向量与 Wiki 页面」

- 检索**必须**携带 `user_id`，为空即拒绝；
- 合并范围**只有**公共 Workspace 与本人 Vault；
- 返回结果附声明置信度与证据 ID，供质检 Agent 溯源；
- 低置信信息在输出时做**风险标注**（三、P5）。

---

## 9. 开发自检清单

- [ ] 新增页面类型是否落在八要素之内？有没有自造本体要素？
- [ ] 专有必填字段是否齐全？status 取值是否匹配类型？
- [ ] 公共页面有没有混入 `user_id`？私有页面 `user_id` 是否与账号一致？
- [ ] 检索是否只合并「公共 + 本人」？有没有遍历他人 Vault？
- [ ] 矛盾处理是否只写 `invalid_at` 而未删除旧声明？
- [ ] 编译产物的三个文件名是否与计划书一致？
- [ ] digest 超限时是否显式标记 `truncated`？
- [ ] 低置信报告是否沿用 0.82 而未新造阈值？

---

## 10. 验证

| 条款 | 验证方式 |
|---|---|
| 八要素与必填字段 | `tests/test_agent_wiki_store.py::test_claim_requires_u17_fields` |
| 公共页禁带 user_id | `::test_public_workspace_page_must_not_carry_user_id` |
| 私有库跨账号不可见 | `::test_cross_account_pages_are_invisible` |
| 检索不跨账号 | `::test_retrieval_merges_workspace_and_own_vault_only` |
| 三份产物 | `tests/test_agent_wiki_compiler.py::test_compile_produces_the_three_plan_artifacts` |
| U17 字段齐全 | `::test_claims_jsonl_rows_carry_u17_fields` |
| 矛盾不删旧声明 | `::test_contradiction_is_reported_and_old_claim_survives` |
| 低置信沿用 0.82 | `::test_low_confidence_uses_plan_threshold_not_a_new_number` |
| digest 上限 | `::test_digest_respects_plan_size_limit` |
