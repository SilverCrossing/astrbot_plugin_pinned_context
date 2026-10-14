"""核心迁移、群权限和两级注入回归测试。"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Logger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


class _Filter:
    def __getattr__(self, _):
        return lambda *args, **kwargs: lambda method: method


class _Star:
    def __init__(self, context):
        self.context = context


class MessageType(Enum):
    FRIEND_MESSAGE = "friend"
    GROUP_MESSAGE = "group"
    OTHER_MESSAGE = "other"


class TextPart:
    def __init__(self, text):
        self.text = text
        self.is_temp = False

    def mark_as_temp(self):
        self.is_temp = True
        return self


_module("astrbot")
_module("astrbot.api", AstrBotConfig=dict, logger=_Logger())
_module("astrbot.api.event", AstrMessageEvent=object, filter=_Filter())
_module("astrbot.api.platform", MessageType=MessageType)
_module("astrbot.api.provider", ProviderRequest=object)
_module("astrbot.api.star", Context=object, Star=_Star)
_module("astrbot.core")
_module("astrbot.core.agent")
_module("astrbot.core.agent.message", TextPart=TextPart)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrbot_plugin_pinned_context.block_manager import BlockManager  # noqa: E402
from astrbot_plugin_pinned_context.db import init_db  # noqa: E402
from astrbot_plugin_pinned_context.injector import ContextInjector  # noqa: E402
from astrbot_plugin_pinned_context.main import PinnedContextPlugin  # noqa: E402


def test_strong_header_keeps_conflict_rules():
    header = ContextInjector.BLOCK_HEADER
    assert "安全防护、文件操作、网络访问、用户配置、隐私权限" in header
    assert "以更严格的一方为准" in header
    assert "知识内容、对话风格、人设、语气、输出格式" in header
    assert "以钉选强约束为准" in header
    assert "无法明确分类时，优先保障安全" in header
    assert "群规则优先于个人规则" in header
    assert "以创建更晚的块为准" in header


def test_injection_uses_status_without_displaying_it():
    injector = ContextInjector()
    blocks = [
        {"status": "active", "level": "strong", "label": "active", "content": "A"},
        {"status": "pinned", "level": "strong", "label": "pinned", "content": "B"},
        {"status": "idle", "level": "strong", "label": "idle", "content": "C"},
    ]
    output = injector.format_strong([], blocks)
    assert "【个人·active】A" in output
    assert "【个人·pinned】B" in output
    assert "【个人·idle】C" not in output
    assert "🔒" not in output


class _Result:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchall(self):
        return self.cursor.fetchall()

    def fetchone(self):
        return self.cursor.fetchone()

    def scalar(self):
        row = self.fetchone()
        return row[0] if row else None


class _Session:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, statement, params=None):
        return _Result(self.connection.execute(statement.text, params or {}))

    async def commit(self):
        self.connection.commit()


def _factory(connection):
    @asynccontextmanager
    async def open_session():
        yield _Session(connection)

    return open_session


def _plugin(connection):
    plugin = object.__new__(PinnedContextPlugin)
    plugin.block_mgr = BlockManager(_factory(connection))
    plugin.injector = ContextInjector()
    plugin.enable_in_group = True
    plugin.group_whitelist = set()
    plugin.group_shared_admin_ids = {"admin"}
    plugin.group_strong_user_ids = set()
    plugin.token_warning_threshold = 1000
    return plugin


class _Event:
    unified_msg_origin = "platform:GroupMessage:group-a"

    def __init__(self, sender="alice", group="group-a", kind=MessageType.GROUP_MESSAGE):
        self.sender = sender
        self.group = group
        self.kind = kind

    def get_message_type(self):
        return self.kind

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.sender

    def is_admin(self):
        return self.sender == "admin"


class _Request:
    def __init__(self):
        self.system_prompt = "原始系统规则"
        self.extra_user_content_parts = []


def test_old_database_migrates_once():
    async def scenario():
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            "CREATE TABLE pinned_blocks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
            "label TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'active', idle_since TEXT, "
            "llm_call_count INTEGER NOT NULL DEFAULT 0, "
            "priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, UNIQUE(session_id, label));"
        )
        for key, label in (("private-umo", "private"), ("group-umo::u:alice", "group")):
            connection.execute(
                "INSERT INTO pinned_blocks "
                "(session_id,label,content,created_at,updated_at) "
                "VALUES (?,?,?,? ,?)",
                (key, label, label, "2026-01-01", "2026-01-01"),
            )
        session = _Session(connection)
        await init_db(session)
        assert dict(connection.execute("SELECT label,level FROM pinned_blocks")) == {
            "private": "strong",
            "group": "context",
        }
        await init_db(session)
        assert (
            connection.execute("SELECT COUNT(*) FROM pinned_blocks").fetchone()[0] == 2
        )

    asyncio.run(scenario())


def test_group_injection_isolated_and_authorized():
    async def scenario():
        connection = sqlite3.connect(":memory:")
        await init_db(_Session(connection))
        plugin = _plugin(connection)
        alice = _Event()
        bob = _Event(sender="bob")
        alice_key, shared_key, reason = plugin._scope_keys(alice)
        bob_key, _, _ = plugin._scope_keys(bob)
        assert reason == "group" and alice_key != bob_key
        manager = plugin.block_mgr
        assert (
            await manager.add(shared_key, "version", "Python 3.12", level="strong")
        )["ok"]
        assert (
            await manager.add(shared_key, "background", "公共背景", level="context")
        )["ok"]
        assert (await manager.add(alice_key, "style", "简短回复", level="context"))[
            "ok"
        ]
        assert (await manager.add(bob_key, "secret", "Bob 的内容", level="context"))[
            "ok"
        ]
        assert (await manager.add(alice_key, "old-strong", "不应生效", level="strong"))[
            "ok"
        ]

        request = _Request()
        await plugin.on_llm_request(alice, request)
        assert "Python 3.12" in request.system_prompt
        assert "不应生效" not in request.system_prompt
        assert len(request.extra_user_content_parts) == 1
        part = request.extra_user_content_parts[0]
        assert part.is_temp
        assert "公共背景" in part.text and "简短回复" in part.text
        assert "Bob 的内容" not in part.text

        assert "没有群个人强约束写入权限" in await plugin._run_command(
            alice, alice_key, "add-strong", "new 约束", False
        )
        assert "只有 AstrBot 管理员" in await plugin._run_command(
            alice, shared_key, "add", "new 背景", True
        )
        admin = _Event(sender="admin")
        assert "已钉选" in await plugin._run_command(
            admin, shared_key, "add-strong", "rule 管理员规则", True
        )
        admin_key, _, _ = plugin._scope_keys(admin)
        assert "已钉选" in await plugin._run_command(
            admin, admin_key, "add-strong", "personal 管理员个人规则", False
        )
        admin_request = _Request()
        await plugin.on_llm_request(admin, admin_request)
        assert admin_request.system_prompt.index("管理员个人规则") < (
            admin_request.system_prompt.index("管理员规则")
        )

    asyncio.run(scenario())


def test_missing_group_id_fails_closed_and_level_independent_of_status():
    async def scenario():
        connection = sqlite3.connect(":memory:")
        await init_db(_Session(connection))
        plugin = _plugin(connection)
        assert plugin._scope_keys(_Event(group="")) == ("", "", "no_group")
        assert plugin._scope_keys(_Event(kind=MessageType.OTHER_MESSAGE)) == (
            "",
            "",
            "unknown_type",
        )
        assert plugin._scope_keys(_Event(kind=MessageType.FRIEND_MESSAGE)) == (
            "",
            "",
            "unknown_type",
        )
        key, _, _ = plugin._scope_keys(_Event())
        manager = plugin.block_mgr
        await manager.add(key, "example", "内容", level="context")
        await manager.set_status(key, "example", "pinned")
        await manager.set_level(key, "example", "strong")
        listed = await manager.list_all(key)
        assert listed["pinned"][0]["level"] == "strong"

    asyncio.run(scenario())


def test_private_default_and_literal_pinned_content():
    async def scenario():
        connection = sqlite3.connect(":memory:")
        await init_db(_Session(connection))
        plugin = _plugin(connection)
        private = _Event(kind=MessageType.FRIEND_MESSAGE, group="")
        private.unified_msg_origin = "platform:FriendMessage:alice"
        key, shared, reason = plugin._scope_keys(private)
        assert reason == "private" and not shared
        assert "已钉选" in await plugin._run_command(
            private, key, "add", "version Python 3.12", False
        )
        assert "已钉选" in await plugin._run_command(
            private, key, "add", "literal -- text --pinned", False
        )
        assert "已钉选" in await plugin._run_command(
            private, key, "add-context", "note 临时资料 --pinned", False
        )
        listed = await plugin.block_mgr.list_all(key)
        assert {b["label"] for b in listed["pinned"]} == {"note"}
        assert (
            next(b for b in listed["active"] if b["label"] == "literal")["content"]
            == "text --pinned"
        )
        request = _Request()
        await plugin.on_llm_request(private, request)
        assert "Python 3.12" in request.system_prompt
        assert "临时资料" in request.extra_user_content_parts[0].text

    asyncio.run(scenario())


def test_add_respects_cap_and_duplicate_label():
    async def scenario():
        connection = sqlite3.connect(":memory:")
        await init_db(_Session(connection))
        manager = BlockManager(_factory(connection), max_blocks=1)
        assert (await manager.add("session", "one", "content"))["ok"]
        duplicate = await manager.add("session", "one", "again")
        assert not duplicate["ok"]
        second = await manager.add("session", "two", "content")
        assert not second["ok"] and "上限" in second["error"]
        assert await manager.count("session") == 1

    asyncio.run(scenario())
