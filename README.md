# Pinned Context Blocks — 钉选上下文块插件

> AstrBot 插件：让用户在对话中手动锚定关键信息，不会被滑动窗口截断。

## 命令速查

```
/pin add <标签> <内容>            新增块（默认 active）
/pin add <标签> <内容> --pinned   新增为长期锚定块
/pin list                          列出所有块
/pin remove <标签>                 删除块
/pin edit <标签> <新内容>          编辑块
/pin idle <标签>                   停用（→ idle，后续自动清理）
/pin activate <标签>               重新激活闲置块
/pin pin <标签>                    长期锚定（永不过期）
/pin unpin <标签>                  取消锚定
/pin clear                         清空全部（管理员 + 需确认）
/pin help                          显示帮助
```

## 三种状态

| 状态 | 含义 | 注入？ | 清理策略 |
|------|------|--------|----------|
| 🟢 active | 正常使用中 | ✅ 每次注入 | 不自动清理 |
| 💤 idle | 用户手动停用 | ❌ 不注入 | 闲置 ≥7 天 + 又过了 ≥10 轮 → 自动删除 |
| 🔒 pinned | 长期锚定 | ✅ 每次注入 | 仅手动删除 |

## 典型用例

```
/pin add Python版本 "3.12 only, 不用 typing.Optional"
/pin add 代码规范 "ruff + mypy, 行宽 100"
/pin pin 项目框架 "FastAPI + SQLAlchemy async"
```

## 安装

将本文件夹放入 AstrBot 的 `data/plugins/` 目录，重启即可。

配置项通过 AstrBot WebUI 管理面板调整（基于 `_conf_schema.json` 自动生成）。

## 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `max_blocks_per_session` | 20 | 每个会话最多钉选块数 |
| `max_label_length` | 20 | 标签最大长度 |
| `max_content_length` | 500 | 内容最大长度 |
| `idle_retention_days` | 7 | idle 块保留天数 |
| `idle_retention_calls` | 10 | idle 块保留 LLM 调用次数 |
| `token_warning_threshold` | 1000 | token 警告阈值（0=不检查） |

## 设计理念

- **System Prompt 之后注入**：安全约束不被覆盖，免疫滑动窗口截断
- **纯手动管理**：第一期不做 AI 自动管理
- **闲置自动清理**：避免僵尸块永久残留
- **20 个上限**：防止 token 膨胀
- **管理员保护**：`/pin clear` 需要管理员权限
