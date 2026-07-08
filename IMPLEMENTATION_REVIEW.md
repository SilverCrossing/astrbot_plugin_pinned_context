# 钉选上下文块插件 — 实施审查报告

> 审查日期：2026-07-08  
> 对照文档：`pinned_context_blocks_design.md` + AstrBot 官方 API 文档  
> 代码版本：v0.1.0  
> 审查范围：全部源码文件（main.py / block_manager.py / db.py / injector.py / metadata.yaml / plugin_config.yml）  
> 参考来源：[AstrBot 官方文档](https://docs.astrbot.app/) · [GitHub 源码](https://github.com/AstrBotDevs/AstrBot) · [DeepWiki Context API](https://deepwiki.com/AstrBotDevs/AstrBot/2.4-context-api-for-plugins) · [DeepWiki Database Architecture](https://deepwiki.com/AstrBotDevs/AstrBot/9.1-database-architecture)

---

## 一、完成度总览

| 设计阶段 | 状态 | 实现情况 |
|----------|------|----------|
| P0 MVP — CRUD + 注入 + `/pin` 命令 | ✅ 完成 | add / list / remove / edit / idle / activate / pin / unpin / clear / help 全部实现 |
| P0 — 上下文注入 | ⚠️ 实现但有隐患 | 追加到 system prompt 末尾，但官方文档警告此方式会破坏 prompt 缓存（见 BUG-3） |
| P0 — 数据库建表 | ✅ 完成 | `init_db` 建表 + 两个索引（但使用原生 SQL 而非官方推荐的 SQLModel） |
| P0 — 配置读取 | ❌ 完全失效 | `context.get_config()` 读取的是全局配置，非插件配置。`plugin_config.yml` 从未被加载（见 BUG-1） |
| P1 — idle 自动清理 | ✅ 完成 | `purge_stale_idle` 双条件（天数 + 调用数）清理 |
| P1 — 会话调用计数 | ✅ 完成 | `increment_call_count` 每次请求递增 idle 块计数 |
| P1 — token 预算警告 | ⚠️ 部分实现 | `_cmd_list` 中有估算逻辑，但配置项未接入（因 BUG-1 导致配置完全不工作）、估算公式与设计文档不一致 |
| P2 — scope 字段（全局/用户级块） | ❌ 未做 | 设计标注为"可选"，符合预期 |
| P3 — Web 面板管理 | ❌ 未做 | 设计标注为"可选"，符合预期 |

**总体评价**：P0 + P1 核心功能骨架完整，但存在 **1 个致命 Bug（配置完全失效）** 和多个 API 使用偏差，需对照官方文档修正后方可正常使用。

---

## 二、Bug 清单

### 🔴🔴 BUG-1（致命）：配置 API 用法错误 — 所有配置项均不生效

**文件**：`main.py` 第 43 行

```python
cfg = context.get_config() or {}
```

**问题**：根据 [AstrBot 官方文档](https://docs.astrbot.app/dev/star/plugin.html)，`context.get_config()` 返回的是 **AstrBot 全局默认配置**，而非当前插件的配置。插件的配置应通过 `__init__` 的 `config: AstrBotConfig` 参数接收：

```python
# 官方推荐写法
from astrbot.api import AstrBotConfig

class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # AstrBotConfig 继承自 Dict
```

**影响**：`plugin_config.yml` 中定义的 `max_blocks_per_session`、`max_label_length`、`max_content_length`、`idle_retention_days`、`idle_retention_calls`、`token_warning_threshold` **全部不生效**，代码始终使用 `cfg.get(key, default)` 中的默认值。用户在 WebUI 中修改配置也不会有任何效果。

**修复建议**：
```python
from astrbot.api import AstrBotConfig

class PinnedContextPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.injector = ContextInjector()
        self.block_mgr = BlockManager(
            db_session_factory=self._get_db,
            max_blocks=config.get("max_blocks_per_session", 20),
        )
        self.block_mgr.MAX_LABEL_LEN = config.get("max_label_length", 20)
        self.block_mgr.MAX_CONTENT_LEN = config.get("max_content_length", 500)
        self.block_mgr.IDLE_RETENTION_DAYS = config.get("idle_retention_days", 7)
        self.block_mgr.IDLE_RETENTION_CALLS = config.get("idle_retention_calls", 10)
        self.token_warning_threshold = config.get("token_warning_threshold", 1000)
```

---

### 🔴 BUG-2（严重）：数据库访问使用私有属性 `_db` 而非公开 API

**文件**：`main.py` 第 64 行

```python
async with self.context._db.get_db() as session:
```

**问题**：`context._db` 是私有属性（下划线前缀）。根据 [AstrBot 源码](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/star/context.py)，`Context` 类提供了**公开方法** `get_db()` 返回 `BaseDatabase` 实例，而 `BaseDatabase` 自身也有 `get_db()` 方法返回 `AsyncSession` 上下文管理器。

**正确的调用链**：
```
context.get_db()  →  BaseDatabase 实例
    .get_db()     →  AsyncSession 上下文管理器
```

**修复建议**：
```python
@asynccontextmanager
async def _get_db(self):
    """获取数据库会话（通过 AstrBot 公开 API）。"""
    async with self.context.get_db().get_db() as session:
        yield session
```

---

### 🔴 BUG-3（严重）：注入 `system_prompt` 可能破坏 prompt 缓存

**文件**：`main.py` 第 84-87 行 / `injector.py`

**问题**：根据 [AstrBot 官方文档](https://docs.astrbot.app/dev/star/plugin.html)，向 `req.system_prompt` 追加**会变化的内容**会破坏模型服务端的 prompt 缓存，**可能增加 7-20 倍的费用**。

官方推荐：
- `req.system_prompt`：适合追加**稳定、长期**的角色设定
- `req.extra_user_content_parts`：适合每轮**变化的动态上下文**

钉选块介于两者之间——大部分时间稳定，但用户执行 `/pin add`、`/pin remove`、`/pin edit` 等操作时会变化。一旦变化，缓存的 system_prompt 失效，后续所有请求都会产生缓存 miss 开销。

**修复建议**：考虑改用 `req.extra_user_content_parts` 注入，使用 `TextPart`：
```python
from astrbot.core.agent.message import TextPart

@filter.on_llm_request()
async def on_llm_request(self, event, req: ProviderRequest):
    blocks = await self.block_mgr.get_active(session_id)
    if blocks:
        text = self.injector.format(blocks)
        req.extra_user_content_parts.append(TextPart(text=text))
```

> **权衡**：`extra_user_content_parts` 不保证不被 `ContextManager` 截断（需验证），而 `system_prompt` 天然免疫截断。如果钉选块变化频率低（用户大部分时间在对话而非管理块），则 `system_prompt` 注入的开销可接受。**建议在文档中明确标注此权衡**。

---

### 🔴 BUG-4：`set_status` 不检查块是否存在

**文件**：`block_manager.py` 第 98-119 行

**问题**：`set_status` 执行 UPDATE 后未检查 `rowcount`，即使块不存在也返回 `{"ok": True}`。用户执行 `/pin idle 不存在的标签` 会收到成功提示。

**对比**：`remove`（第 75 行）和 `update`（第 92 行）都正确检查了 `result.rowcount == 0`。

**修复建议**：
```python
result = await session.execute(...)
await session.commit()
if result.rowcount == 0:
    return {"ok": False, "error": f"块'{label}'不存在"}
return {"ok": True, "label": label, "status": new_status}
```

---

### 🔴 BUG-5：`set_status` 进入 idle 时未重置 `llm_call_count`

**文件**：`block_manager.py` 第 105-110 行

**问题**：当块从 active → idle 时，`llm_call_count` 应重置为 0（代表"进入 idle 后经过的调用数"）。当前不重置，如果块经历 `idle → activate → idle` 循环，计数从旧值继续累加，导致块被过早清理。

**复现场景**：
1. 创建块，`/pin idle` → `llm_call_count` 从 0 开始递增
2. 5 轮后 `/pin activate` → `llm_call_count` 仍为 5
3. 再次 `/pin idle` → `llm_call_count` 从 5 继续递增
4. 只需 5 轮即达阈值 10，比预期的 10 轮少一半

**修复建议**：idle 分支加 `llm_call_count = 0`：
```python
if new_status == "idle":
    await session.execute(
        "UPDATE pinned_blocks SET status = ?, idle_since = ?, "
        "llm_call_count = 0, updated_at = ? "
        "WHERE session_id = ? AND label = ?",
        (new_status, now, now, session_id, label),
    )
```

---

### 🟡 BUG-6：`_cmd_add` 的 `--pinned` 解析有边界问题

**文件**：`main.py` 第 140-142 行

**问题**：使用 `rest.endswith("--pinned")` 检测标志。如果内容恰好以 `--pinned` 结尾（如 `/pin add 标签 内容--pinned`），会被误判为标志，内容被截断。

**修复建议**：用正则匹配 ` --pinned$`（前面有空格）：
```python
import re
match = re.search(r'\s--pinned\s*$', rest)
if match:
    pinned = True
    rest = rest[:match.start()].strip()
```

---

### 🟢 BUG-7：`add` 方法不校验 `status` 参数合法性

**文件**：`block_manager.py` 第 27-30 行

**问题**：`add` 方法接受任意 `status` 字符串，未校验是否为 `active` / `idle` / `pinned`。

**修复建议**：
```python
if status not in ("active", "idle", "pinned"):
    return {"ok": False, "error": f"无效状态: {status}"}
```

---

### 🟢 BUG-8：`clear` 命令同时删除 pinned 块，无额外警告

**文件**：`main.py` 第 227-232 行

**问题**：`/pin clear` 会删除所有块包括 pinned 块。用户可能期望 pinned 块被保护。

**修复建议**：在确认提示中区分 pinned 块数量，或要求 `clear --force` 才删除 pinned 块。

---

## 三、API 符合性审查（对照 AstrBot 官方文档）

以下问题基于 [AstrBot 官方插件开发文档](https://docs.astrbot.app/dev/star/plugin.html) 和 [源码](https://github.com/AstrBotDevs/AstrBot) 确认：

### 偏差-1：配置文件格式应为 `_conf_schema.json` 而非 `plugin_config.yml`

**官方规范**：AstrBot 插件应使用 `_conf_schema.json`（JSON Schema 格式）定义配置项。框架会根据 schema 自动生成 WebUI 配置界面。`plugin_config.yml` 是旧格式。

**当前状态**：使用 `plugin_config.yml`，可能无法在 WebUI 中正确显示配置面板。

**修复建议**：创建 `_conf_schema.json` 替代 `plugin_config.yml`：
```json
{
  "max_blocks_per_session": {
    "description": "每个会话最多钉选块数",
    "type": "int", "default": 20
  },
  "max_label_length": {
    "description": "单个块标签最大长度",
    "type": "int", "default": 20
  },
  "max_content_length": {
    "description": "单个块内容最大长度",
    "type": "int", "default": 500
  },
  "idle_retention_days": {
    "description": "idle 块保留天数",
    "type": "int", "default": 7
  },
  "idle_retention_calls": {
    "description": "idle 块保留 LLM 调用次数",
    "type": "int", "default": 10
  },
  "token_warning_threshold": {
    "description": "注入后总 token 警告阈值（0=不检查）",
    "type": "int", "default": 1000
  }
}
```

---

### 偏差-2：`metadata.yaml` 缺少推荐字段

**官方规范**：`metadata.yaml` 支持以下字段：

| 字段 | 说明 | 当前状态 |
|------|------|----------|
| `name` | 插件名 | ✅ 有 |
| `desc` | 描述 | ✅ 有 |
| `author` | 作者 | ✅ 有 |
| `version` | 版本 | ✅ 有 |
| `display_name` | 展示名（v4.5.0+） | ❌ 缺失 |
| `short_desc` | 短描述 | ❌ 缺失 |
| `support_platforms` | 支持平台列表 | ❌ 缺失 |
| `astrbot_version` | AstrBot 版本范围约束（PEP 440） | ❌ 缺失 |

**建议**：补充 `astrbot_version` 字段以声明兼容版本，避免在不适用的版本上加载。

---

### 偏差-3：数据库建表应使用 SQLModel 而非原生 SQL

**官方规范**：根据 [AstrBot 数据库架构文档](https://deepwiki.com/AstrBotDevs/AstrBot/9.1-database-architecture)，AstrBot 使用 **SQLModel**（SQLAlchemy + Pydantic）定义数据表，通过 `SQLModel.metadata.create_all` 自动建表。

**当前状态**：使用原生 SQL 字符串（`CREATE TABLE ...`）。虽然通过 SQLAlchemy session 执行原生 SQL 也能工作，但不符合官方推荐模式。

**建议**：迁移到 SQLModel 模型定义，与 AstrBot 核心保持一致：
```python
from sqlmodel import SQLModel, Field

class PinnedBlock(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    session_id: str
    label: str
    content: str = ""
    status: str = "active"
    # ...
```

---

### 偏差-4：群聊权限控制有官方 API 可用

**官方 API**：`@filter.permission_type(filter.PermissionType.ADMIN)` 可限制命令仅管理员可用。

**当前状态**：所有 `/pin` 命令对所有人开放。

**建议**：至少对 `/pin clear` 加权限控制：
```python
@filter.command("pin")
@filter.permission_type(filter.PermissionType.ADMIN)
async def pin_command(self, event: AstrMessageEvent):
    ...
```

或拆分为子命令级别控制（`clear` 需管理员，其余开放）。

---

### 偏差-5：`llm_call_count` 语义与设计文档不一致

| 维度 | 设计文档（2.1 / 2.2） | 实现代码 |
|------|----------------------|----------|
| 含义 | "本会话已发生的 LLM 调用累计数"（全局快照） | "进入 idle 后又经过的 LLM 调用数"（每块独立递增） |
| 计算方式 | 进入 idle 时存快照，清理时用 `current_count - snapshot` | 每次 LLM 调用递增 idle 块计数，清理时直接比较 `>= 阈值` |

**评价**：实现方式更简洁，逻辑正确。但设计文档需同步更新。

---

### 偏差-6：token 估算公式不一致

| 来源 | 公式 |
|------|------|
| 设计文档 5.8 | "中文字符数 ≈ tokens"（1:1） |
| 实现代码 | `est_tokens = est_chars * 2` |

**建议**：统一为更准确的估算，或在文档中标注"粗略估算"。

---

### 偏差-7：`DESIGN.md` 与 `pinned_context_blocks_design.md` 内容完全重复

两个文件内容**一模一样**（510 行）。

**建议**：删除其一。

---

### 偏差-8：缺少 `__init__.py`

`main.py` 使用相对导入（`from .block_manager import ...`），但无 `__init__.py`。

**建议**：添加空 `__init__.py` 确保包导入兼容性。

---

## 四、潜在漏洞与风险

### 风险-1：群聊无权限控制

任何群员可执行所有 `/pin` 命令。官方提供了 `@filter.permission_type(filter.PermissionType.ADMIN)` API（见偏差-4），应加以利用。

---

### 风险-2：无数据库迁移机制

`init_db` 只做 `CREATE TABLE IF NOT EXISTS`。AstrBot 核心使用 `PRAGMA table_info` + `ALTER TABLE` 做"软迁移"（见 [sqlite.py](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/db/sqlite.py)）。建议参考此模式。

---

### 风险-3：无单元测试

整个项目无测试文件。建议至少覆盖 `BlockManager` 核心逻辑。

---

### 风险-4：关键操作缺少日志

只有 `add` 和 `purge_stale_idle` 有日志。`remove`、`update`、`set_status`、`clear` 均无日志，不利于审计。

---

### 风险-5：每次 LLM 调用产生 3 次数据库往返

`on_llm_request` 中 `increment_call_count` → `purge_stale_idle` → `get_active` 各自独立开关 DB session。建议合并为单事务，或加内存缓存。

---

### 风险-6：SQL DEFAULT 时间格式与代码写入格式不一致

| 来源 | 格式 |
|------|------|
| SQL DEFAULT `datetime('now')` | `2026-07-08 12:57:14`（空格分隔，无时区） |
| Python `datetime.isoformat()` | `2026-07-08T12:57:14.123456+00:00`（T 分隔，有时区） |

当前 `add()` 总是显式传入 Python 端时间，DEFAULT 未被使用，**不会**导致运行时 Bug。但存在未来代码路径依赖 DEFAULT 引入格式不一致的风险。建议移除 DEFAULT 或统一格式。

---

### 风险-7：备选方案 — 使用 Star 基类内置 KV Store

AstrBot 的 `Star` 基类内置了 `PluginKVStoreMixin`，提供简单的键值存储：
- `self.put_kv_data(key, value)`
- `self.get_kv_data(key, default)`
- `self.delete_kv_data(key)`

对于简单场景可替代自建数据库表。但钉选块需要复杂查询（按 status 过滤、排序、分组），KV Store 不够用，自建表是合理的。**可作为设计文档中的对比说明**。

---

## 五、改进建议

### 短期（v0.1.1 — 致命问题修复）
1. **修复 BUG-1**：改用 `config: AstrBotConfig` 参数接收插件配置（**最高优先级**）
2. **修复 BUG-2**：`_db` → `get_db().get_db()` 使用公开 API
3. **修复 BUG-4**：`set_status` 加 `rowcount` 检查
4. **修复 BUG-5**：`set_status` idle 分支重置 `llm_call_count = 0`
5. 修复 BUG-6：`--pinned` 参数解析改用正则
6. 修复 BUG-7：`add` 方法校验 `status` 参数
7. 创建 `_conf_schema.json` 替代 `plugin_config.yml`
8. 补充 `metadata.yaml` 的 `astrbot_version` 字段
9. 添加 `__init__.py`
10. 删除重复设计文档

### 中期（v0.2 — 健壮性增强）
1. **评估 BUG-3**：是否迁移到 `extra_user_content_parts` 注入（需验证截断行为）
2. 群聊权限控制（利用 `@filter.permission_type`）
3. 迁移到 SQLModel 模型定义
4. 核心逻辑单元测试
5. 数据库迁移机制（参考 AstrBot 核心的 `PRAGMA table_info` 模式）
6. 补全关键操作日志
7. 同步设计文档（`llm_call_count` 语义、token 估算公式）

### 长期（v0.3+ — 功能扩展）
1. `scope` 字段（session / user / global）
2. LLM 辅助管理（`manage_pin` 工具 + pending 审批流）
3. 块模板变量
4. Web 面板管理

---

## 六、文件清单

| 文件 | 行数 | 说明 | 状态 |
|------|------|------|------|
| `main.py` | 267 | 插件主体 | ⚠️ 配置 API 错误 + DB 私有属性访问 |
| `block_manager.py` | 208 | CRUD + 状态管理 | ⚠️ set_status 两个 Bug |
| `db.py` | 37 | 建表 + 索引 | ✅ 可用（建议迁移 SQLModel） |
| `injector.py` | 37 | 上下文注入 | ✅ 可用（需评估注入位置） |
| `metadata.yaml` | 5 | 插件元信息 | ⚠️ 缺少推荐字段 |
| `plugin_config.yml` | 17 | 配置默认值 | ❌ 格式过时 + 从未被代码读取 |
| `_conf_schema.json` | — | 配置 Schema | ❌ 缺失（需新建） |
| `__init__.py` | — | 包初始化 | ❌ 缺失 |
| `README.md` | 47 | 用户文档 | ✅ 可用 |
| `DESIGN.md` | 510 | 设计文档 | ⚠️ 与下方文件重复 |
| `pinned_context_blocks_design.md` | 510 | 设计文档 | ⚠️ 与上方文件重复 |

---

## 七、审查结论

插件整体架构清晰，模块职责分明，P0 + P1 功能骨架完整。但对照 AstrBot 官方文档后发现了**致命问题**：

1. **配置系统完全失效**（BUG-1）：`context.get_config()` 读取的是全局配置而非插件配置，导致 `plugin_config.yml` 中所有配置项从未生效。这是最高优先级修复项。

2. **数据库访问不符合官方 API**（BUG-2）：使用了私有属性 `_db`，应改用公开方法 `context.get_db().get_db()`。

3. **system_prompt 注入有性能隐患**（BUG-3）：官方文档明确警告向 system_prompt 追加变化内容会破坏 prompt 缓存，可能增加 7-20 倍费用。需评估是否迁移到 `extra_user_content_parts`。

4. **配置文件格式过时**：应从 `plugin_config.yml` 迁移到 `_conf_schema.json`，否则 WebUI 配置面板可能无法正确显示。

5. **set_status 有两个逻辑 Bug**（BUG-4/5）：不检查存在性 + 不重置计数。

建议 v0.1.1 优先修复 BUG-1 ~ BUG-5，确保插件基本可用后再推进功能增强。

---

> 报告结束。如需对任何条目展开讨论或要求修复，请告知。
