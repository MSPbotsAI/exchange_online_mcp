# 模块：`src/exo_mcp/tools/mailbox.py`（邮箱读写/隐藏状态）

本模块文档的第一条记录（PRD-20321 之前这个仓库没有任何已积累的模块事实）。

## PRD-20321 · 2026-10-12

- `exo_set_mailbox_hidden`（`src/exo_mcp/tools/mailbox.py:174-230` @ `fedf853`）过去的做法是：通过 `Set-Mailbox` 写入 `HiddenFromAddressListsEnabled`，然后立刻调用一次 `Get-Mailbox`，不管读到什么都直接返回——没有延迟、没有重试。Exchange Online 的收件人目录变更（包括这个属性）是众所周知的"写入后立即读不一定能看到"，而这份代码之前完全没处理过这种滞后。本单之前，谁撞上这个滞后，拿到的就是一个看起来权威、实际上滞后的 `false`/`true`，而且没办法跟真的失败区分开。
- 修复方式是新增一个辅助函数 `_read_mailbox_until_hidden_matches`（`:61-90` @ `fedf853`）：最多重读 `_HIDDEN_VERIFY_ATTEMPTS`（目前是 4）次，每次间隔 `_HIDDEN_VERIFY_BACKOFF_S`（目前是 0.2 秒），一旦读回值与请求的状态匹配就立刻停止。如果预算耗尽还没匹配上，返回结果会带上 `verified: false`、`attempts`，以及一句提示调用方用 `exo_get_mailbox` 稍后重新核查的 `note`——不会把滞后的值当作已确认的结果呈现。
- **这个重试预算（4 次 / 0.2 秒间隔）是占位值，不是实测出来的。** 还没有人针对真实租户的传播延迟做过测量（见 `mcp-service-gateway` 仓库上 `docs/tickets/PRD-20321.md` 的「交接」一节）。以后如果要调这个预算，要改的就是 `_read_mailbox_until_hidden_matches` 正上方的这两个常量。
- 返回结果的形状是向后兼容的：在匹配上的那条路径（也就是常见情况）里，返回结果跟本单之前完全一样——不会多出任何新字段。只有在重试预算耗尽时，才会出现 `verified`/`attempts`/`note` 这几个字段。
- 测试覆盖：`tests/test_tool_calls.py::test_set_mailbox_hidden_confirms_on_first_readback`、`::test_set_mailbox_hidden_retries_until_readback_matches`、`::test_set_mailbox_hidden_reports_unverified_after_retry_budget`、`::test_set_mailbox_hidden_reports_not_found`（回归保护——对一个不存在的 identity，写入步骤本身的失败行为不受这次新加的读取循环影响）。既有的 `test_set_mailbox_hidden_reports_the_read_back_value` 未作任何修改，依然通过。
- 为此加了一个可复用的测试写法：`_FlipAfterNReads`（只在 `tests/test_tool_calls.py` 本地生效，没有改 `conftest.py`）——这是一个 `dict.get()` 的替身，让一个测试能模拟某个 cmdlet 在同一个测试里被多次调用时返回不同的结果，而共享的 `Upstream` fixture 本身不支持这个（它对每个 cmdlet 在整个测试期间只支持一个固定的返回值）。以后如果还有测试需要"先滞后、后正确"或类似的多次读取序列，应该复用这个写法，而不是去改 `conftest.py` 里的 `Upstream`（那样会影响套件里所有其它测试）。
