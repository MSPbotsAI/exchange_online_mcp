# 模块：`src/exo_mcp/tools/groups.py`（分发组成员管理）

本文件首次建立（此文件之前没有累积事实）。

## PRD-20394 · 2026-10-13

- 新增 `exo_add_distribution_group_member`（`:49-134` @ `c0d53ea`，工作分支 `dev01.mbagent/PRD-20394`，写这条记录时尚未合入 `main`——本仓库没有 `int` 分支，没有中间 ref 可引用；合入后请重新核对这个 path:line）。按工单明确要求，参数形状与 `exo_remove_distribution_group_member` 保持一致。
- **幂等性靠一次预检成员读取（`Get-DistributionGroupMember`）实现，而不是解析 `Add-DistributionGroupMember` 自身的错误文案。** 这是相对于更便宜的"解析错误文案"方案的刻意选择：Exchange 对这个 cmdlet 的"成员已存在"错误没有任何确认过的文案（见 `docs/tickets/PRD-20394.md` § 技术设计，方案选型——三种受限组类型错误文案里有两种同样未经确认，这也是为什么那两种同样用预检判定而不是靠猜文案）。如果以后有工单在真实租户上确认了实际文案，可以考虑去掉预检改为直接捕获错误——但没有确认之前不要这么改，那会重新引入这个设计本来要避开的风险。
- **组类型限制通过 `classify_group_restriction()` 判定**（`src/exo_mcp/tools/_common.py`，纯函数，不依赖传输层），用 `Get-Recipient` 的 `RecipientTypeDetails`/`IsDirSynced` 字段，判定顺序是：本地同步优先（信号最具体），然后动态分发组，最后 Microsoft 365 组（`GroupMailbox`）。顺序很重要，因为一个同步对象的 `RecipientTypeDetails` 看起来仍然可能像普通分发组。
- **存在一个已知且可接受的 TOCTOU 竞态**：在成员预检读取和 `Add-DistributionGroupMember` 写入之间，两个并发调用都可能通过预检。`_is_already_member()`（`:32-43`）和 `:126-131` 处的兜底（用 `_DUPLICATE_MEMBER_MARKERS` 匹配写入调用自身的错误，尽力而为，非主要机制）覆盖了常见情况，但**不保证**能捕住真实租户上所有可能的文案——见 `docs/tickets/PRD-20394.md` § 技术设计，非功能（幂等/并发）。匹配不上的竞态会表现为普通的 `upstream_error`，而不是 `already_member`。对一个管理/自动化工具而言这是可以接受的，不是高 QPS 路径。
- **成员匹配大小写不敏感**，比对 `Name`/`PrimarySmtpAddress`/`Alias`/`Guid`（`_MEMBER_FIELDS`，`:14-19`）——任意一个与传入的 `member` 字符串匹配即视为已是成员。
- 测试：`tests/test_groups_add_member.py`，每条 AC 一条（TC-EXO-001 至 TC-EXO-008），外加两条补充：大小写不敏感匹配、三个受限类型错误码两两不同。
