# 模块：`src/exo_mcp/tools/permissions.py`（邮箱/收件人权限授予）

本模块文档的第一条记录（本仓之前没有为这个文件积累过事实）。

## PRD-20480 · 2026-10-14

- 新工具 `exo_add_sendonbehalf_permission`（`src/exo_mcp/tools/permissions.py` @ `3ec7afb`），放在 `exo_add_mailbox_permission` 之后。用增量写法 `Set-Mailbox -GrantSendOnBehalfTo @{Add=[delegate]}`——和 `exo_add_mailbox_permission` 用的 `Add-MailboxPermission`/FullAccess 是不同的权限、不同的 cmdlet：SendOnBehalf 让邮件显示为"代表"这个邮箱发出，而不是授予对这个邮箱的完全访问权。已有的被委托人保持不变（是增量,不是整体替换）。沿用 `mailbox.py` 里那些 `Set-Mailbox` 类工具的 `verified: false` + 复制延迟提示模式。
- 这是本模块第一个用到多值 `{"Add": [...]}` cmdlet 参数的工具——本文件此前所有工具（`exo_add_mailbox_permission`、`exo_add_recipient_permission` 及它们的 `Remove-*` 对应物）设的都是固定的标量 `AccessRights` 值。还没有在真实 EXO REST 端点上验证过（和 `mailbox.py` 里 `exo_add_email_alias` 是同一个开放问题，对应 TC-EXO-004）；如果被拒绝，文档里给的后备方案是靠 `exo_get_mailbox` 读出全量列表再 `Set-Mailbox` 整体写回。
- 不需要新的 Exchange 角色——`Recipient Management`（本仓现有的唯一必需角色，README 里写的）本来就覆盖 `Set-Mailbox`，所以这个工具没有跨仓库的 scope 授权依赖，和同一张单里 `ms-graph-mcp` 那半边的 `graph_create_tap`/`graph_add_group_owner` 不一样。
- 测试覆盖：`tests/test_tool_calls.py::test_add_sendonbehalf_permission_grants_delegate`、`::test_add_sendonbehalf_permission_not_found`（均为本单新增，在 `3ec7afb` 上全部通过）。
- 自动化测试未覆盖的部分（需要真实租户——见 `docs/tickets/PRD-20480.md` § 自测，TC-EXO-001b）：真实 EXO 租户上 `GrantSendOnBehalfTo` 重复 `Add` 是否真的会无副作用地跳过（增量=幂等的假设，尚未确认），以及复制完成后 `exo_get_mailbox` 是否能看到这个被委托人。

## PRD-20480-r2 · 2026-10-19（取代上面的 PRD-20480 记录）

- 真实租户 INT 测试（comment `90170264109063`，Minnie Zhou，2026-10-19）回答了上面那条记录标出的开放问题：真实 EXO REST 端点**确实拒绝** `{"Add": [...]}` 这种线上格式，报 `invalid_argument "Invalid Operation"`。`exo_add_sendonbehalf_permission`（`src/exo_mcp/tools/permissions.py` @ `a43db49`）现在用文档里早就写好的后备方案：先 `Get-Mailbox` 读当前 `GrantSendOnBehalfTo`，用 `Get-Recipient` 校验 `delegate` 能解析到真实收件人（解析不到就拒绝为 `invalid_argument`），大小写不敏感地检查是否已在列表上（命中则直接跳过写入），否则把完整合并后的列表用一次 `Set-Mailbox` 写回，再 `Get-Mailbox` 回读一次确定 `verified`。
- 返回形状变了：新增了 `result: "added"|"already_present"` 字段，不再只靠一个裸的 `granted: true` 作为唯一信号——修复前的形状即便在真实租户上实际失败的那次调用也会报 `granted: true`，这正是 Minnie 的 comment 能把它判成 FAIL（而不是一个无伤大雅的返回形状细节）的原因。
- 从 `mailbox.py` 导入 `_read_mailbox`，而不是重复实现第二个 `Get-Mailbox` 投影辅助函数——这是本仓这两个工具模块之间第一次跨文件导入。
- 测试覆盖：`tests/test_tool_calls.py::test_add_sendonbehalf_permission_grants_delegate` 因新调用序列改写（判定句不变，mocked cmdlet 变了）；`::test_add_sendonbehalf_permission_not_found` 现在由 `Get-Mailbox` 触发，不再是 `Set-Mailbox`（D1 是先读后写）；新增：`::test_add_sendonbehalf_permission_already_present_skips_write`、`::test_add_sendonbehalf_permission_rejects_delegate_that_does_not_exist`、`::test_add_sendonbehalf_permission_merges_into_existing_list`。
- 自动化测试仍未覆盖的部分（需要真实租户——见 `docs/tickets/PRD-20480-r2.md` § 自测，TC-EXO-013）：新的读取-合并-写回兜底方案本身在真实租户上能否正确往返，以及 comment 里提到的 `2qmxyw` 没有共享邮箱是否会影响复测。没有编造成"已通过"。

## PRD-20480-r3 · 2026-10-19（取代上面的 PRD-20480-r2 记录）

- 第二轮 INT 测试（comment `90170264132422`，Minnie Zhou，2026-10-19）发现复制窗口内重复调用 `exo_add_sendonbehalf_permission` 第二次仍然报 `added`，而不是 `already_present`。根因：写入复制完成后，EXO 把 `GrantSendOnBehalfTo` 的条目存成委托人的 object id（INT 实测是类似 `c1dcd1cd-...` 的 GUID），但 already_present 判断（修复前 `:171-175`）只比对了原始 `delegate` 字符串、`Get-Recipient` 的 `Identity`、以及它的 `PrimarySmtpAddress`——从未比对过这个 id。已修复（N3，`src/exo_mcp/tools/permissions.py` @ `0600f42`）：判断现在还会把 `Get-Recipient` 的 `ExternalDirectoryObjectId` 拿去跟当前列表比对。
- 工具 docstring（N3 + N4）现在说明：复制窗口内的重复调用可能仍报 `added`，对同一邮箱的下一次写入应等待复制完成或先用 `exo_get_mailbox` 确认。加上这句提示后，这个工具与 `mailbox.py` 的 `exo_add_email_alias` 的描述都需要重新裁剪，才能满足本仓 `test_tools_list_snapshot` 的 500 字符上限（`tests/test_tools.py:69`）。
- 测试覆盖：`tests/test_tool_calls.py::test_add_sendonbehalf_permission_already_present_by_object_id`（新增，N3）。
- 仍未在真实租户的实际复制时序上独立验证过（需要真实租户——见 `docs/tickets/PRD-20480-r3.md` § 自测，TC-EXO-017）：单元测试证明的是比对逻辑本身，不是真实世界的竞态窗口。本沙箱没有可用的 EXO 凭据。
