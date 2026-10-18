# Jev 决策模块开发规范

> **计划书依据**：三、P0-1「输出三份配套规范：《Jev 决策模块开发规范》…」
> **定位**：本文档约束一切与 Jev 相关的调用、训练、模板与降级实现。
> **产出阶段**：P0-1（契约）／P3（路由接入）

---

## 1. 冻结条款（不得变更）

| 编号 | 条款 | 出处 |
|---|---|---|
| J-01 | Jev **只输出结构化枚举标签 + 置信概率**，禁止输出自由自然语言文本 | 一、项目概述；三、P3 |
| J-02 | 固定分类任务（意图识别、多意图冲突检测、重规划条件、风险判定）优先 Jev 结构化输出 | 七、验收标准 15 |
| J-03 | 置信阈值 `jev_confidence_threshold = 0.82`；低于阈值自动向下降级 | 八；七、16 |
| J-04 | `jev_fallback_enable = true`：Jev 不可用或置信不足时向下游路由流转 | 八 |
| J-05 | `jev_shadow_mode = true`：支持影子模式并行对比原有路由逻辑，**结果只记录不参与决策** | 八；七、16 |
| J-06 | Jev 服务故障时系统可降级兼容原有路由 | 七、16 |
| J-07 | Jev 处于五层路由的**第二层**，位于硬规则之后、向量相似度之前 | 三、P3 |
| J-08 | 多意图冲突**禁止系统自动选优先级**，必须暂停分发并向用户确认 | 八 `multi_intent_auto_resolve=False` |
| J-09 | **Jev 调用独立配额统计**，与普通 LLM 调用分开计 | 三、P3 |

---

## 2. 调用契约

实现：`ai_butler/infra/llm_gateway.py` 的 `JevClient`。

```python
decision = await JevClient(...).decide(
    user_id=state["user_id"],          # 必填：禁止不带用户身份调用
    question="帮我安排国庆去杭州的三天行程",
    allowed_labels=INTENT_LABELS,      # 必填：候选标签集（见第 3 节）
)

decision.label        # 枚举标签，或 None（未采用）
decision.confidence   # 0~1
decision.source       # "jev" | "fallback"
decision.shadow       # 是否处于影子模式
decision.accepted     # 等价于 source == "jev"
```

**契约要求**：

1. `user_id` 必填非空，否则拒绝调用（计划书七、11）；
2. 调用走统一网关，自动获得**独立配额**、熔断、防雪崩与重试；
3. 解析必须**严格**：仅接受形如 `{"label": "...", "confidence": 0.x}` 的 JSON。
   解析失败、标签不在候选集、置信值非法 —— **一律判无效并降级**，
   绝不把自由文本放行到下游（J-01）。

---

## 3. 标签集的来源（不得自造）

**计划书未给出标签清单**（裁定台账 U01），因此：

- **禁止**在代码中内置任何意图标签常量；
- 标签集必须由调用方以 `allowed_labels` 传入；为空即抛错并提示人工补充；
- 标签体系借鉴 `home-assistant/intents`（CC-BY-4.0）的
  `domain → intent → slot → slot_combinations` 分层结构；
  其仓库已含官方 zh-CN 句式可直接参考命名；
- 降级路径复用其 `no_intent` 语义位（「抱歉，我无法理解您的意思…」）。

---

## 4. 模型本体（裁定 U02）

走「**受限小模型**」路线，不采用受限大模型：

| 项 | 取值 |
|---|---|
| 基座 | Qwen2.5-0.5B / 1.5B（Apache-2.0） |
| 输出约束 | JSON Schema 硬约束（本地 Ollama `format`，或 outlines / xgrammar） |
| 理由 | 约束解码保证「只出枚举」在工程上可强制（outlines 官方称 guaranteed schema compliance） |

**注意**：vLLM v0.12.0 起已删除 `guided_json` 等旧字段，改用 `structured_outputs`；
Ollama 的结构化输出**云端不支持**，只能本地跑。

---

## 5. ⚠ 置信度来源禁令（裁定 U15，最容易出错的一条）

**禁止**把约束解码的 token 概率直接当作 Jev 的置信度。

原因：约束解码会 mask 掉非法 token，**概率分布被扭曲**，不再是校准后的概率。
若直接使用，`0.82` 阈值会形同虚设，而且**不会报错**——只是判定悄悄失准。

**正确做法**：把两件事拆开——

1. **约束解码只负责输出格式合法**（标签一定是候选集内的枚举值）；
2. **置信度由独立路径产出**：分类头概率，或独立校准集 + 温度缩放。

并借鉴 CLINC150 的 out-of-scope 类实现「**拒识 → 降级**」：
标签集之外的输入应当落入 `fallback`，而不是被硬塞进某个标签。

---

## 6. 多意图冲突的处理（裁定 U14）

计划书要求「多意图冲突禁止系统自动选优先级，必须向用户确认」。
把这条约束**编码进模型输出**，而不是靠后置规则判断：

| 字段 | 取值 | 含义 |
|---|---|---|
| `require_clarification` | 0 / 1 | 是否必须向用户澄清 |
| `clarifying_question` | 字符串 | 要问用户的问题 |
| `category` | FD / MC / LA | 澄清类型 |
| `subclass` | WHOM / WHAT / WHEN / WHERE / UNFAMILIAR / CONTRADICTION / SEMANTIC / LEXICAL | 细分原因 |

字段设计借鉴 CLAMBER（ACL 2024）。**该仓库无 LICENSE 文件**，
因此**只借字段设计与分类法，不得分发其数据**，商用前须联系作者。

**多意图命中时**：Jev 返回后路由层立即暂停分发、向用户确认，不向下游流转。

---

## 7. 降级矩阵

| 情形 | 行为 | 依据 |
|---|---|---|
| `jev_enable=false` | 直接返回 fallback | 八 |
| 网关全部供应商不可用 | 返回 fallback，reason 注明降级 | J-06 |
| 输出不是合法 JSON | 判无效 → fallback | J-01 |
| 标签不在候选集 | 判无效 → fallback | J-01 |
| 置信 < 0.82 且 `jev_fallback_enable=true` | 向下游流转 | J-03 / J-04 |
| `jev_shadow_mode=true` | 计算但**不采用**，`shadow=True` 记录 | J-05 |
| 多意图冲突 | 暂停分发，向用户确认 | J-08 |

---

## 8. 开发自检清单

- [ ] 有没有任何路径让 Jev 输出自由文本进入下游？
- [ ] 标签集是否由调用方传入？代码里有没有硬编码枚举？
- [ ] 置信度是否被误取自约束解码的 token 概率？
- [ ] 阈值是否仍是 0.82？降级开关是否仍是 true？影子模式是否仍开启？
- [ ] 多意图冲突是否会导致系统自动选主意图？
- [ ] 是否所有调用都携带了 `user_id`？是否走了独立配额？
- [ ] 影子模式下是否确实没有采用 Jev 结果？

---

## 9. 验证

| 条款 | 验证方式 |
|---|---|
| J-01 只出枚举 | `tests/test_llm_gateway.py::test_jev_never_passes_free_text_downstream` |
| J-03 阈值降级 | `::test_jev_below_threshold_falls_back_downstream` |
| J-05 影子模式 | `::test_jev_shadow_mode_records_but_does_not_decide` |
| J-06 故障降级 | `::test_jev_fault_degrades_smoothly` |
| 标签集不得自造 | `::test_jev_requires_label_set_from_human` |
| 冻结参数 | `tests/test_config.py::test_jev_frozen_params_match_plan_section_eight` |
