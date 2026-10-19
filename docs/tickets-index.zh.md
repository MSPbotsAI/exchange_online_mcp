# 工单索引

这个仓库对于多仓库工单不单独存一份工单文档——本仓做了什么见 `docs/modules/README.zh.md`。设计/自测文档本身落在工单被指定的主仓库里。

| 工单 | 标题 | 状态 | 文档（主仓库） |
|---|---|---|---|
| PRD-20321 | Connector gaps in Graph, CW, EXO（本仓部分：`exo_set_mailbox_hidden` 新增带重试的 `verified` 标记，commit `fedf853`） | 冻结 @ fedf853 | [EN](https://github.com/MSPbotsAI/mcp-service-gateway/blob/dev01.mbagent/PRD-20321/docs/tickets/PRD-20321.md) · [中文](https://github.com/MSPbotsAI/mcp-service-gateway/blob/dev01.mbagent/PRD-20321/docs/tickets/PRD-20321.zh.md) |
| PRD-20480 | 补齐 M365 onboarding 缺失的工具——本仓部分：`exo_add_sendonbehalf_permission`、`exo_add_email_alias`；第一轮（`3ec7afb`）以增量方式交付了两个工具，但用的 `{"Add": [...]}` 线上格式被 2026-10-19 针对真实租户的 INT 测试发现会被真实 EXO REST 端点拒绝；第二轮（`PRD-20480-r2.md`，commit `a43db49`）把两者都改成文档里早就写好的读取-合并-写回兜底方案，外加写入前校验（D1-D4），仍需真实租户复测（TC-EXO-013）；本工单横跨两个仓库，`ms-graph-mcp` 上也提交了一份内容完全一致的文档 | 冻结 @ a43db49（第二轮；第一轮冻结 @ 3ec7afb） | [EN](https://github.com/MSPbotsAI/exchange_online_mcp/blob/dev01.mbagent/PRD-20480/docs/tickets/PRD-20480-r2.md) · [中文](https://github.com/MSPbotsAI/exchange_online_mcp/blob/dev01.mbagent/PRD-20480/docs/tickets/PRD-20480-r2.zh.md)（第一轮，已被取代：[EN](https://github.com/MSPbotsAI/exchange_online_mcp/blob/dev01.mbagent/PRD-20480/docs/tickets/PRD-20480.md) · [中文](https://github.com/MSPbotsAI/exchange_online_mcp/blob/dev01.mbagent/PRD-20480/docs/tickets/PRD-20480.zh.md)） |
