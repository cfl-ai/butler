-- AI 管家 Agent 生态体系 —— PostgreSQL 初始化
-- 依据：《AI管家Agent生态体系项目计划书》三、P1「对接 Agent-Wiki v2 本体」
--                                       三、P2「BM25 + ChromaDB 向量库」
-- 范围限制：本文件只启用 pgvector 扩展。
--          计划书【八】未定义关系型数据库选型，P1 四大核心数据表的字段规范
--          属 P1 交付物，此处不预置任何业务表，避免违反 Skill-01。

CREATE EXTENSION IF NOT EXISTS vector;
