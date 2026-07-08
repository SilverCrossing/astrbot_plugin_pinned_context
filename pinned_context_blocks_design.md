# Pinned Context Blocks 插件设计文档

> 状态：草稿  
> 日期：2026-07-08  
> 目标平台：AstrBot  

---

## 一、概念定义

**Pinned Context Blocks（钉选上下文块）** 是一种由用户手动管理的、数量有限（建议 ≤20）的、在滑动窗口/上下文压缩之上持久存在的结构化上下文锚点。

### 1.1 解决什么问题

```
System Prompt        ← 全局、永久的。改一次影响所有对话。
    │
    │         【巨大的空白地带】
    │         没有任何机制来存放"重要但非全局"的信息
    │
    ▼
Sliding Window       ← 临时、自动的。说截断就截断。
```

钉选块填在这个空白地带：比 System Prompt 轻量（可按会话切换），比对话历史持久（不会被截断）。

### 1.2 典型用例

| 场景 | 钉选块示例 |
|------|-----------|
| 编程协作 | `📌 Python版本: 3.12 only, 不用 typing.Optional` |
| 写作项目 | `📌 世界观: 蒸汽朋克，魔法已衰落` |
| 跑团 DM | `📌 艾伦 HP 8/20，麻痹中` |
| 长期研究 | `📌 论文A结论: X方法在Y条件下无效` |
| 日常对话 | `📌 用户偏好: 回复简洁，不用颜文字` |

---

## 二、数据结构

### 2.1 钉选块

```python
@dataclass
class PinnedBlock:
    id: int              # 自增主键
    session_id: str      # 会话 ID（群聊/私聊隔离）
    label: str           # 标签，如"Python版本"
    content: str         # 内容，如"3.12 only"
    status: str = "active"       # "active" | "idle" | "pinned"
    idle_since: str | None = None  # 进入 idle 的时间 ISO
    llm_call_count: int = 0       # 进入 idle 后又经过的 LLM 调用数（每次 on_llm_request 递增）
    priority: int = 0    # 排序优先级，越大越靠前
    created_at: str      # 创建时间 ISO
    updated_at: str      # 更新时间 ISO
```

**三种状态**：

| 状态 | 含义 | 注入？ | 清理策略 |
|------|------|--------|----------|
| `active` | 正常使用中 | ✅ 每次注入 | 不会自动清理 |
| `idle` | 用户手动停用 | ❌ 不注入 | 满足条件后自动删除 |
| `pinned` | 长期锚定 | ✅ 每次注入 | 仅手动删除 |

**idle 自动清理条件**（两者同时满足）：

```python
# 实现方式：llm_call_count 在每次 on_llm_request 时对 idle 块递增
# 进入 idle 时重置为 0，清理时直接比较 >= 阈值

def should_purge_idle(block, current_time):
    idle_duration = current_time - block.idle_since   # 进入 idle 多久了
    return idle_duration >= IDLE_RETENTION_DAYS and block.llm_call_count >= IDLE_RETENTION_CALLS
```

即：**"闲置超过 N 天 且 又经过了 M 轮 LLM 调用"** 才清理。两个维度结合，避免活跃会话里的短暂闲置块被误删，也避免僵尸会话里的块永久残留。

### 2.2 数据库表

```sql
CREATE TABLE IF NOT EXISTS pinned_blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    label           TEXT    NOT NULL,
    content         TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'active',  -- 'active' | 'idle' | 'pinned'
    idle_since      TEXT,          -- 进入 idle 的时间 ISO，NULL 表示未 idle
    llm_call_count  INTEGER NOT NULL DEFAULT 0,  -- 进入 idle 后经过的 LLM 调用数（进入 idle 时重置为 0）
    priority        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(session_id, label)
);

CREATE INDEX IF NOT EXISTS idx_pinned_session ON pinned_blocks(session_id);
CREATE INDEX IF NOT EXISTS idx_pinned_status ON pinned_blocks(session_id, status);
```

### 2.3 注入到 LLM 的格式

```
【📌 钉选上下文】
【Python版本】Python 3.12 only, 避免 typing.Optional 等弃用写法
【项目结构】使用 src/ 布局，tests/ 镜像源码结构
【代码规范】ruff + mypy，行宽 100

[原始 system prompt 或对话上下文续...]
```

- 只在有激活块（active + pinned）时才注入，不会凭空产生空块标记
- 注入位置：system prompt **末尾**（追加在原有 system prompt 之后，不覆盖安全约束）

---

## 三、模块设计

```
astrbot_plugin_pinned_context/
├── __init__.py                # 包初始化
├── _conf_schema.json          # 配置 Schema（WebUI 自动生成面板）
├── metadata.yaml              # 插件元信息
├── main.py                    # Star 插件主体，生命周期 + 钩子
├── block_manager.py           # 块的 CRUD、状态管理、闲置清理
├── db.py                      # 数据库初始化和迁移
├── injector.py                # 上下文注入逻辑
└── README.md                  # 用户文档
```

### 3.1 `main.py` — 插件主体

**职责**：AstrBot 生命周期管理 + 钩子注册

```python
class PinnedContextPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        self.block_mgr: BlockManager
        self.injector: ContextInjector
    
    async def initialize(self):
        # 初始化数据库
    
    @filter.on_llm_request()
    async def on_llm_request(self, event, req: ProviderRequest):
        session_id = req.session_id
        
        # 1. 递增调用计数 + 清理过期 idle 块
        await self.block_mgr.increment_call_count(session_id)
        await self.block_mgr.purge_stale_idle(session_id)
        
        # 2. 获取 active + pinned 块
        active_blocks = await self.block_mgr.get_active(session_id)
        
        # 3. 注入到 system prompt 末尾
        req.system_prompt = self.injector.inject(
            req.system_prompt or "", active_blocks
        )
    
    @filter.command("pin")
    async def pin_command(self, event: AstrMessageEvent):
        # 子命令：add / list / remove / edit / idle / activate / pin / unpin / clear
    
    # === 预留扩展点 ===
    # 未来如需 LLM 辅助管理块，在此注册 @filter.llm_tool("manage_pin")
    # 详见本文档"预留扩展点"章节
    
    async def terminate(self):
        # 清理资源
```

### 3.2 `block_manager.py` — 核心业务逻辑

**职责**：块的增删改查、状态管理、闲置清理、数量限制、验证

```python
class BlockManager:
    MAX_BLOCKS = 20               # 每个会话最多 20 个块
    MAX_LABEL_LEN = 20            # 标签最长 20 字符
    MAX_CONTENT_LEN = 500         # 内容最长 500 字符
    IDLE_RETENTION_DAYS = 7       # idle 保留天数
    IDLE_RETENTION_CALLS = 10     # idle 保留 LLM 调用数
    
    async def add(session_id, label, content, status="active") -> Block
    async def remove(session_id, label) -> bool
    async def update(session_id, label, content=None) -> Block
    async def set_status(session_id, label, new_status) -> Block  # active/idle/pinned
    async def list_all(session_id) -> dict[str, list[Block]]       # 按 status 分组
    async def get_active(session_id) -> list[Block]                # active + pinned
    async def purge_stale_idle(session_id) -> int                  # 清理过期 idle 块
    async def increment_call_count(session_id)                     # LLM 调用计数
    async def clear(session_id) -> int
    async def count(session_id) -> int
```

### 3.3 `injector.py` — 上下文注入

**职责**：将 active + pinned 的块格式化为 LLM 可读文本，追加到 system prompt 末尾

```python
class ContextInjector:
    BLOCK_HEADER = "\n\n【📌 钉选上下文】"
    BLOCK_TEMPLATE = "【{label}】{content}"
    
    def format(self, blocks: list[Block]) -> str:
        """将块列表格式化为注入文本，追加到 system prompt 后"""
        active_blocks = [b for b in blocks if b.status in ("active", "pinned")]
        if not active_blocks:
            return ""
        lines = [self.BLOCK_HEADER]
        for b in sorted(active_blocks, key=lambda x: -x.priority):
            pinned_mark = " 🔒" if b.status == "pinned" else ""
            lines.append(self.BLOCK_TEMPLATE.format(
                label=b.label + pinned_mark, content=b.content
            ))
        return "\n".join(lines)
    
    def inject(self, system_prompt: str, blocks: list[Block]) -> str:
        """将钉选块追加到 system prompt 末尾"""
        block_text = self.format(blocks)
        if not block_text:
            return system_prompt
        return system_prompt + block_text
```

### 3.4 `db.py` — 持久化层

**职责**：建表、迁移

```python
async def init_db(db_session):
    """创建表和索引"""
    await db_session.execute(CREATE_TABLE_SQL)
    await db_session.execute(CREATE_INDEX_SQL)
    await db_session.commit()
```

---

## 四、命令设计

### 4.1 用户命令

```
/pin add <label> <content>              新增钉选块（默认 active）
/pin add <label> <content> --pinned     新增为长期锚定块
/pin list                                列出所有块（按状态分组）
/pin remove <label>                      删除指定块
/pin edit <label> <new_content>          编辑块内容
/pin idle <label>                        停用块（active/idle → idle）
/pin activate <label>                    重新激活闲置块（idle → active）
/pin pin <label>                         锚定块（→ pinned，永不清除）
/pin unpin <label>                       取消锚定（pinned → active）
/pin clear                               清空所有块（需确认）
/pin help                                显示帮助
```

状态切换流：

```
                    /pin idle
    active ──────────────────→ idle
      ↑                          │
      │   /pin activate          │ 闲置 ≥7天 + 又过了 ≥10轮
      └──────────────────────────┘ → 自动删除
      │
      │   /pin pin
      ▼
    pinned ←→ /pin unpin → active
   (永不过期)
```

### 4.2 LLM 工具（可选）

```python
manage_pin(action="suggest", label="线索A", content="暗影教会在哨塔")
# → pending 状态，用户通过 /pin approve 确认
```

---

## 五、边界情况与问题

### 5.1 数量上限

**问题**：用户可能创建过多块，超出 token 预算或管理复杂度。

**方案**：
- 硬上限 20 个块（可配置）
- 达到上限时 `/pin add` 返回错误提示
- 建议用户 `/pin list` 检查并清理不用的块

### 5.2 标签冲突

**问题**：同一会话下已有同名标签。

**方案**：
- `label` 在 `(session_id, label)` 上有 UNIQUE 约束
- `/pin add` 时若标签已存在，提示"已存在同名块，使用 /pin edit 修改或 /pin remove 删除后重建"
- 或者提供 `--force` 覆盖选项

### 5.3 闲置清理 vs TTL

**问题**：如何平衡"不丢失有用信息"和"不积累僵尸块"？

**方案**：废弃简单 TTL 递减，改用 `active / idle / pinned` 三态 + 闲置自动清理。

- `active`：正常使用，每次注入，永不清除
- `idle`：用户手动停用，保留但满足条件后自动清除
- `pinned`：长期锚定，永不清除（直到用户手动删除或取消锚定）

**闲置清理双条件**（同时满足才清除）：

```
闲置天数 ≥ IDLE_RETENTION_DAYS（默认 7 天）
    且
进入 idle 后经过的 LLM 调用次数 ≥ IDLE_RETENTION_CALLS（默认 10 次）
```

设计理由：
- 只用天数：活跃会话（每天聊天）里闲置的块不会被清除，合理
- 只用轮次：僵尸会话（一个月没聊）里闲置的块会永久残留，不合理
- 两者结合：活跃会话靠轮次保护，僵尸会话靠天数清理

清理操作在每个 `on_llm_request` 触发时执行一次轻量检查。

### 5.4 状态切换命令

**问题**：群聊 A 的钉选块不应出现在群聊 B 的上下文中。

**方案**：
- 使用 AstrBot 的 `session_id` 做隔离（已内置在 `ProviderRequest` 中）
- 数据库查询始终带上 `WHERE session_id = ?`
- 私聊和群聊自动分离（AstrBot 使用不同的 session_id）

### 5.5 上下文注入位置

**问题**：钉选块应该放在 system prompt 之前还是之后？

**方案**：放在 system prompt **之后**（即 system prompt 末尾追加）。

**理由**：
- 用户钉选的上下文块是"当前任务的参考信息"，不是"角色身份定义"
- System prompt 可能包含安全约束（如"下载前需确认"、"私密信息需审查"），钉选块不应绕过
- 角色扮演需求应走 AstrBot 的 Persona 系统，而非 Context Blocks
- 注入后顺序：`[原始 system prompt] → [钉选块] → [对话上下文]`

```
[原始 system prompt: 安全规则 + 角色设定]
    ↓
【📌 钉选上下文】
【Python版本】Python 3.12 only
【项目规范】ruff + mypy
    ↓
[对话历史 + 用户消息]
```

### 5.6 会话隔离

**问题**：用户可能塞入超长内容，占用过多 token。

**方案**：
- 单个块内容限制 500 字符（可配置）
- `/pin add` 时做长度校验，超限提示截断或拒绝
- 可选：支持 `/pin add --allow-long` 管理员绕过限制

### 5.7 并发安全

**问题**：TTL 递减和用户同时编辑同一块。

**方案**：
- AstrBot 消息是串行处理的（单 worker），并发问题较轻
- TTL 递减使用 `UPDATE ... WHERE ttl > 0` 原子操作
- 如果未来支持多 worker，使用 SQLite WAL 模式 + 乐观锁

### 5.8 token 预算

**问题**：钉选块也占 token，不能无限增长。

**方案**：
- 注入前计算激活块的总 token 数（粗略估算：中文字符数 ≈ tokens）
- 如果超过阈值（如 1000 tokens），在 `/pin list` 中警告
- 用户可手动停用低优先级的块来腾空间

### 5.9 全局块 vs 会话块

**问题**：某些信息应该跨会话共享（如用户偏好），某些仅限当前会话（如任务临时约束）。

**方案**：
- 第一期只做会话级隔离（`session_id`）
- 第二期可加 `scope` 字段：`session` / `user` / `global`
  - `session`：当前会话有效
  - `user`：同一用户的所有会话有效
  - `global`：所有用户的所有会话有效（需管理员权限）

### 5.10 与现有上下文管理器的兼容性

**问题**：AstrBot 的 `ContextManager` 做截断/压缩时，可能影响钉选块。

**方案**：
- 钉选块**不进入 `contexts` 列表**，而是注入到 `system_prompt`
- System prompt 不在 `ContextManager` 的截断/压缩范围内
- 这样钉选块天然免疫截断

---

## 六、潜在风险

### 6.1 用户滥用
- 风险：用户塞入大量无意义内容污染上下文
- 缓解：数量上限 + 长度限制 + 内容不可为空

### 6.2 LLM 误解
- 风险：LLM 可能把钉选块当成"必须严格遵守的指令"而过度解读
- 缓解：注入格式中不包含"你必须"等强制性措辞，只作为参考上下文

### 6.3 隐私泄露
- 风险：用户在群聊中钉选的敏感信息会出现在该群所有对话的上下文中
- 缓解：文档中提醒用户不要在公开群聊中钉选私密信息；未来可加可见性控制

### 6.4 性能影响
- 风险：每次 LLM 调用都查一次数据库
- 缓解：TTL 递减只需 `UPDATE` 一条 SQL；读取可加简单内存缓存（TTL 递减时失效）

---

## 七、配置项（plugin_config.yml）

```yaml
# 每个会话最多钉选块数
max_blocks_per_session: 20

# 单个块标签最大长度
max_label_length: 20

# 单个块内容最大长度
max_content_length: 500

# idle 块保留天数（闲置超过此天数才考虑清理）
idle_retention_days: 7

# idle 块保留 LLM 调用次数（又经过了此轮次才清理）
idle_retention_calls: 10

# 注入后总 token 警告阈值（0=不检查）
token_warning_threshold: 1000
```

---

## 八、实施路线

| 阶段 | 内容 | 预估工时 |
|------|------|----------|
| P0 MVP | 基本 CRUD + 注入 + `/pin` 命令（add/list/remove/edit/idle/activate/pin/unpin/clear） | 2 天 |
| P1 增强 | idle 自动清理 + 会话调用计数 | 0.5 天 |
| P1 增强 | token 预算警告 | 0.5 天 |
| P2 可选 | 全局/用户级块（scope 字段） | 1 天 |
| P3 可选 | Web 面板管理 | 2 天 |

---

## 九、预留扩展点

以下功能不在第一期范围，但架构已预留接口，未来可平滑接入：

### 9.1 LLM 辅助管理块

```python
# 预留接口：在 main.py 中已预留注释标记
# @filter.llm_tool(name="manage_pin")
# async def manage_pin(self, event, action, label, content=""):
#     ...
```

**流程设计（供未来参考）**：
1. LLM 在对话中发现值得钉选的信息 → 调用 `manage_pin(action="suggest", ...)`
2. 系统写入 `pending_pins` 表（而非直接生效）
3. 下次 `/pin list` 时显示待审批的建议
4. 用户通过 `/pin approve <label>` 确认 → 正式写入 `pinned_blocks`

**不做的原因**：
- 增加每次 LLM 调用的 token 开销（工具 schema 注入）
- LLM 自主判断"该钉选什么"的可靠性未经验证
- 先观察纯手动模式的使用情况，再评估是否需要 AI 辅助

### 9.2 块模板

```python
# 预留：BlockManager 中 add() 的 content 参数可接受模板变量
# 如 "Python {version} only" → 通过 /pin add Python版本 --vars version=3.12
```

### 9.3 跨会话共享

```python
# 预留：PinnedBlock 新增 scope 字段
# scope: "session" | "user" | "global"
# 数据库层面只需加一列，BlockManager 的查询方法加 scope 过滤参数
```

---

## 十、参考与致谢

- **MemGPT / Letta**: Memory Blocks 概念的来源
- **dnd5e_helper**: 跑团场景中上下文管理的实践参考
- **AstrBot CountLocPlugin**: 插件结构参考
- **Context Engineering**: 上下文工程领域的最佳实践

---

> 下一步：确认本设计文档后，可开始搭建 `astrbot_plugin_pinned_context` 骨架代码。
