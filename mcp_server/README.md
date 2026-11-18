# AiButler MCP Server

把 ai-butler 的工具调度层挂上 **MCP（Model Context Protocol）标准协议**——任何 MCP 客户端（WorkBuddy / Claude / Cursor 等）都能直接调用 butler 的只读工具。

## 工具清单（以运行时注册表为准）

| 工具 | 说明 | 依赖 |
|---|---|---|
| `butler_search_materials` | 素材中心检索（规划/问答前的事实来源） | 素材向量库（懒加载） |
| `butler_read_wiki_page` | 读取 Agent Wiki 一页（公共/本人私有） | Wiki 存储（懒加载） |
| `butler_get_weather` | 城市天气实况+预报（wttr.in 主源 + Open-Meteo 备源，failover） | 外网可达 |
| `butler_search_poi` | 高德 POI 检索 | `AMAP_KEY` 环境变量 |

缺依赖的工具**不注册、不暴露**——清单里没有就是没有，不静默降级。

## 快速开始

MCP 客户端配置（以 WorkBuddy 为例，写入 `mcp.json` 的 `mcpServers`）：

```json
"ai-butler-tools": {
  "command": "D:/PycharmProjects/ai-butler-ecosystem/.venv/Scripts/python.exe",
  "args": ["-m", "mcp_server.server"],
  "env": { "BUTLER_USER_ID": "u_mcp_001" },
  "cwd": "D:/PycharmProjects/ai-butler-ecosystem"
}
```

环境变量：

- `BUTLER_USER_ID`（必配）：MCP 会话的数据边界。**user_id 不在工具参数契约里**，由服务端注入——调用方传了也会被覆盖，这是 butler 的反幻觉纪律（"读谁的数据"不能交给概率决定）
- `AMAP_KEY`（可选）：配了才有 `butler_search_poi`

## 架构：继承而非旁路

```
MCP 客户端 ──stdio──▶ mcp_server.server（本目录）
                          │  契约自检：MCP 签名 ≡ ToolSpec.params_schema（E-44 fail-fast）
                          ▼
                    ToolDispatcher.dispatch   ◀── 权限闸(U43-45) / 重复调用拦截 / 无增量熔断
                          │                        user_id 注入 / 错误码判责(U60) 全部生效
                          ▼
              build_tool_registry（唯一装配点，真值源）
                          ▼
         SearchWiring / LazyWikiStore / wttr.in + Open-Meteo（failover）/ 高德
```

五条设计纪律（详见 `server.py` 模块注释）：不重写工具、不绕过调度器、user_id 不进契约、契约单一真值源、懒装配。

## 运行记录（2026-11-18）

- `pytest tests/test_mcp_server.py`：**4 passed**（条件暴露 / 调度器机制继承 / 判责字段透出 / 契约漂移 fail-fast）；连同既有工具层 39 项回归全过
- MCP stdio 端到端（官方 client 拉起子进程）：`initialize → list_tools → call_tool` 全通；
  工具清单 = 4 项全注册
- **`butler_get_weather('杭州', days=2)` 全绿**：实况 13.6℃ 大致晴朗 + 两天预报
  （小雨 97% 降水概率等），`ok=true`
- `butler_read_wiki_page('nonexistent_page')`：真实返回 `{"found": false}`
  （懒加载 Wiki 构建成功，"没找到"作为结论而非错误返回）

## 判责透出演示（真实事件）

首次冒烟时曾遇到一次瞬时网络波动导致天气请求失败，MCP 端原样收到
`ok=false + error_code=HANDLER_ERROR + 完整 note`——调用方据此识别为
环境问题而非工具缺陷，**没有盲目重试**。复测恢复全绿。这正是一次
"错误即观察"纪律在协议边界上的真实生效记录。

## 天气源 failover（2026-11-18）

后续诊断证实 Open-Meteo 在本机网络下**间歇不可达**（000 超时与恢复交替，
当天三次观测：失败 → 恢复 → 再次失败），而 wttr.in 稳定（200/1.5s）。
据此把天气工具改造为**双源 failover**：`WttrWeather`（wttr.in j1）升主源 +
`OpenMeteoWeather` 降备用源，由 `FailoverWeather` 串联——只切源不切数据，
每一跳都是当下新采集（不违反 U97「禁止回退」），返回带 `provider` 标记。
实弹验证：在 Open-Meteo 被阻断的时刻，`butler_get_weather('杭州', days=2)`
经 wttr.in 全绿（实况 13.0℃ 阴 + 两天预报，provider=wttr.in 可追溯）。
测试：新增 16 项守护测试（j1 解析 / 双码表隔离 / 3 天上限如实报错 /
换源 / 全败列因 / 能力跳过 / 装配顺序），相关回归 123 passed。
