# 模块：`src/exo_mcp/tools/mailbox.py`（邮箱读写/隐藏状态）

本模块文档的第一条记录（PRD-20321 之前这个仓库没有任何已积累的模块事实）。

## PRD-20321 · 2026-10-12

- `exo_set_mailbox_hidden`（`src/exo_mcp/tools/mailbox.py:174-230` @ `fedf853`）过去的做法是：通过 `Set-Mailbox` 写入 `HiddenFromAddressListsEnabled`，然后立刻调用一次 `Get-Mailbox`，不管读到什么都直接返回——没有延迟、没有重试。Exchange Online 的收件人目录变更（包括这个属性）是众所周知的"写入后立即读不一定能看到"，而这份代码之前完全没处理过这种滞后。本单之前，谁撞上这个滞后，拿到的就是一个看起来权威、实际上滞后的 `false`/`true`，而且没办法跟真的失败区分开。
- 修复方式是新增一个辅助函数 `_read_mailbox_until_hidden_matches`（`:61-90` @ `fedf853`）：最多重读 `_HIDDEN_VERIFY_ATTEMPTS`（目前是 4）次，每次间隔 `_HIDDEN_VERIFY_BACKOFF_S`（目前是 0.2 秒），一旦读回值与请求的状态匹配就立刻停止。如果预算耗尽还没匹配上，返回结果会带上 `verified: false`、`attempts`，以及一句提示调用方用 `exo_get_mailbox` 稍后重新核查的 `note`——不会把滞后的值当作已确认的结果呈现。
- **这个重试预算（4 次 / 0.2 秒间隔）是占位值，不是实测出来的。** 还没有人针对真实租户的传播延迟做过测量（见 `mcp-service-gateway` 仓库上 `docs/tickets/PRD-20321.md` 的「交接」一节）。以后如果要调这个预算，要改的就是 `_read_mailbox_until_hidden_matches` 正上方的这两个常量。
- 返回结果的形状是向后兼容的：在匹配上的那条路径（也就是常见情况）里，返回结果跟本单之前完全一样——不会多出任何新字段。只有在重试预算耗尽时，才会出现 `verified`/`attempts`/`note` 这几个字段。
- 测试覆盖：`tests/test_tool_calls.py::test_set_mailbox_hidden_confirms_on_first_readback`、`::test_set_mailbox_hidden_retries_until_readback_matches`、`::test_set_mailbox_hidden_reports_unverified_after_retry_budget`、`::test_set_mailbox_hidden_reports_not_found`（回归保护——对一个不存在的 identity，写入步骤本身的失败行为不受这次新加的读取循环影响）。既有的 `test_set_mailbox_hidden_reports_the_read_back_value` 未作任何修改，依然通过。
- 为此加了一个可复用的测试写法：`_FlipAfterNReads`（只在 `tests/test_tool_calls.py` 本地生效，没有改 `conftest.py`）——这是一个 `dict.get()` 的替身，让一个测试能模拟某个 cmdlet 在同一个测试里被多次调用时返回不同的结果，而共享的 `Upstream` fixture 本身不支持这个（它对每个 cmdlet 在整个测试期间只支持一个固定的返回值）。以后如果还有测试需要"先滞后、后正确"或类似的多次读取序列，应该复用这个写法，而不是去改 `conftest.py` 里的 `Upstream`（那样会影响套件里所有其它测试）。

## PRD-20321 后续 · 2026-10-13（取代上面的重试方案）

- 真实租户实测，上面的重试方案会给出假的「已确认」：同一次工具调用里的读回落到刚执行写入的那台服务器，第一次就读到新值；而独立调用的 `exo_get_mailbox` 60 秒后还是旧值。隐藏、取消隐藏两个方向都有同样的延迟，几分钟后才变成正确值。所以这是目录复制延迟，不是写入失败。
- `exo_set_mailbox_hidden` 不再读回。`Set-Mailbox` 成功后返回 `requested_hidden`、`write_accepted: true`、`verified: false`，加一条 `note`，提示调用方约 5 分钟后用 `exo_get_mailbox` 复查。`_read_mailbox_until_hidden_matches` 和相关常量已删掉。返回里不再有 `hidden_from_address_lists_enabled`，避免调用方把同一次调用里读到的值当成已经复制完的状态。
- 不在工具里轮询：同一次调用里的读回证明不了什么，轮询几分钟又会让 MCP 调用超过网关 / Agent 的超时。复查放到 SOP 后面的步骤做。
- 测试：`test_set_mailbox_hidden_reports_pending_not_verified`、`test_set_mailbox_hidden_unhide_is_also_unverified`、`test_set_mailbox_hidden_reports_not_found`。`_FlipAfterNReads` 随重试测试一起删掉。

## PRD-20394 · 2026-10-13

- 新增 `exo_get_auto_reply`/`exo_set_auto_reply`（`src/exo_mcp/tools/mailbox.py:391-486` @ `c0d53ea`，工作分支 `dev01.mbagent/PRD-20394`，写这条记录时尚未合入 `main`——本仓库没有 `int` 分支，没有中间 ref 可引用；合入后请重新核对这个 path:line）。读取/更新邮箱的自动回复配置；set 调用只改它拿到的字段（依赖 `invoke()` 既有的 None 丢弃机制，`api_client.py:239-241`），并恰好做一次写入后读回。
- **`exo_set_auto_reply` 在写入前不做预检读取**，这与上面的 `exo_set_mailbox_forwarding`（`:216-309`）不同。这是刻意的：工单的 AC 要求每次成功的 `exo_set_auto_reply` 恰好发起一次 `Get-MailboxAutoReplyConfiguration` 调用，加一次预检会变成两次。这个工具的「未找到」改为通过 `Set-MailboxAutoReplyConfiguration` 调用自身的错误文案浮现，由 `_classify()`（`api_client.py:99-106`）分类，与 `exo_set_mailbox_hidden` 已经依赖的机制相同。以后谁要在这里加第四个写邮箱的工具，应该明确自己决定"要不要预检读取"这个问题，而不是照抄随便看到的哪一个邻居——现有这两个工具在这一点上故意不一致，不是疏忽。
- **DateTime 的线上格式是一个未经确认的假设，不是已验证的事实**：`_parse_offset_datetime`/`_datetime_to_z`（`:69-121`）把 `StartTime`/`EndTime` 以带 `Z` 的 UTC ISO-8601 格式发给 `Set-MailboxAutoReplyConfiguration`，`_normalize_upstream_time`（`:74-93`）假设 `Get-MailboxAutoReplyConfiguration` 返回同样格式、可按 UTC 存储理解的值。两者都没有针对真实租户验证过（见 `docs/tickets/PRD-20394.md` § 技术设计，风险 #1）。如果以后真实租户测试发现这个假设错了（就像上面 `PRD-20321 follow-up` 发现隐藏邮箱读回假设错了那样），请用同样的方式更新这条记录——追加取代，不要悄悄改写。
- `external_audience` 的合法取值是字面字符串 `None`/`Known`/`All`（真实 Exchange 枚举）——与 Python 的 `None` 默认值（表示"字段未提供"）是两回事。调用方要显式要求该受众取值时必须传字符串 `"None"`。
- 测试：`tests/test_mailbox_auto_reply.py`，每条 AC 一条（TC-EXO-007 至 TC-EXO-016，TC-EXO-015 覆盖两个工具的未找到情形）。
