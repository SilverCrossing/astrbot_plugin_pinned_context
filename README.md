# Pinned Context Blocks — 钉选上下文块插件

AstrBot 插件：手动保存长期约束和参考资料，在后续 LLM 请求中持续提供相关内容。

## 两级注入

| 级别 | 用途 | 注入位置 |
| --- | --- | --- |
| `strong` | 人设、软件包版本、长期行为规范、群规则 | system prompt 末尾 |
| `context` | 项目背景、临时状况、参考知识、个人偏好 | 本轮额外用户内容（不写入会话历史） |

级别与生命周期状态独立：`active` 正常注入；`idle` 停用并在闲置达到阈值后清理；`pinned` 正常注入且仅手动删除。`pinned` 表示长期保留，不表示高优先级。插件提示词不能代替 AstrBot 的工具和数据访问权限。

## 命令

```text
/pin add <标签> <内容>                 新增个人块（私聊 strong，群聊 context）
/pin add-strong <标签> <内容>          新增个人强约束；群聊需授权
/pin add-context <标签> <内容>         新增个人软上下文
/pin add <标签> <内容> --pinned        新增长期保留块
/pin add <标签> -- <原样内容>          将后面的 --pinned 作为正文保存
/pin level <标签> strong|context      切换注入级别
/pin list                             列出个人块及级别、状态
/pin edit <标签> <新内容>             编辑内容
/pin remove <标签>                    删除块
/pin idle|activate|pin|unpin <标签>    切换生命周期状态
/pin clear                            提示清空数量
/pin clear confirm                    清空个人块
/pin shared <以上子命令>              在群聊中查看或管理群共享块
/pin help                             显示帮助
```

群共享块的列表可供群成员查看；增删改、级别与状态切换、清空仅限 AstrBot 管理员或 `group_shared_admin_ids` 中的发送者。群个人强约束仅限这些管理员或 `group_strong_user_ids` 中的发送者。普通群成员可以管理自己的软上下文块。群共享块在该群所有成员发言时生效，个人块只在本人发言时生效。机器人在群内的回复仍可能透露个人块的信息，因此不要在群块中保存秘密。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `max_blocks_per_session` | 20 | 每个个人或共享作用域的块上限 |
| `max_label_length` | 20 | 标签最大长度 |
| `max_content_length` | 500 | 内容最大长度 |
| `idle_retention_days` | 7 | idle 软清理天数 |
| `idle_retention_calls` | 10 | idle 软清理调用次数 |
| `idle_hard_retention_days` | 30 | idle 最长保留天数 |
| `token_warning_threshold` | 1000 | `/pin list` 的估算 token 提醒阈值，0 为关闭 |
| `enable_in_group` | false | 群聊总开关 |
| `group_whitelist` | `[]` | 可用群 ID 或完整会话标识；空列表表示所有群 |
| `group_shared_admin_ids` | `[]` | 可管理群共享块的发送者 ID |
| `group_strong_user_ids` | `[]` | 可管理群个人强约束的发送者 ID |

群聊默认关闭。管理员权限来自 AstrBot 的 `event.is_admin()` 或插件配置的 ID 白名单。白名单可填发送者 ID（适用于所有已启用的群），也可填 `<umo>::u:<发送者 ID>` 限定到某个群。插件按消息类型区分群聊和私聊；群消息缺少群 ID 或发言人 ID 时拒绝操作与注入。

## 安装与升级

将插件目录放入 AstrBot 的 `data/plugins/`，在 WebUI 配置后重启。此版本需要 **AstrBot v4.24.0 或更高**，以支持软上下文的临时内容标记。

从 v0.2.0 升级时，插件会为旧表增加 `level` 列：旧私聊块保留 `strong`；旧群个人块改为 `context`，避免普通群成员继续拥有 system prompt 写入权。数据库结构迁移只在列不存在时执行，重复启动不会重复转换。建议升级前备份 AstrBot 数据库。旧版群共享键不属于任何用户，不会自动认领为新群共享块。

## 设计说明

- 同类块按创建时间倒序；编辑不会改变顺序。
- 群共享强约束优先于获准的群个人强约束；软上下文中的个人偏好优先于群共享偏好。模型的文字遵循不能替代程序权限。
- idle 块在作用域再次发起 LLM 请求时清理：默认闲置 7 天且又经过 10 次调用，或闲置满 30 天。
- token 提醒目前只在 `/pin list` 显示，不是硬预算。
