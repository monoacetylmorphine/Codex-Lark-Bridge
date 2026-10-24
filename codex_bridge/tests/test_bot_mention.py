"""Group mention identity checks with no credentials or network access."""
from __future__ import annotations
from codex_bridge.middleware import message_router
from types import SimpleNamespace as NS
import os
from unittest.mock import AsyncMock, patch
import pytest
from codex_bridge.configs.loader import CONFIG
import test_stop_command as helpers

class BotMentionTests:

    def setup_method(self) -> None:
        helpers.StopTests.setup_method(self)
        self._pytest_stack.enter_context(patch.dict(os.environ, {}, clear=True))

    def configure(self, **fields):
        self._pytest_stack.enter_context(patch.dict(CONFIG['lark']['credentials'], fields, clear=True))

    def test_union_id_only_survives_renaming(self) -> None:
        self.configure(robotUnionIdEnv='TEST_BOT_ID')
        os.environ['TEST_BOT_ID'] = 'on_bot'
        assert message_router._bot_identity(required=True) == ('on_bot', '')
        assert message_router._is_bot_mentioned([NS(id=NS(union_id='on_bot'), name='renamed')])
        assert not message_router._is_bot_mentioned([NS(id=NS(union_id='on_other'))])

    def test_name_only(self) -> None:
        self.configure(robotNameEnv='TEST_BOT_NAME')
        os.environ['TEST_BOT_NAME'] = 'Bot'
        assert message_router._bot_identity(required=True) == ('', 'Bot')
        assert message_router._is_bot_mentioned([NS(name='Bot')])
        assert not message_router._is_bot_mentioned([NS(name='Other')])

    def test_both_use_or_matching(self) -> None:
        self.configure(robotUnionIdEnv='TEST_BOT_ID', robotNameEnv='TEST_BOT_NAME')
        os.environ.update(TEST_BOT_ID='on_bot', TEST_BOT_NAME='Bot')
        for mention in (NS(id=NS(union_id='on_bot'), name='Renamed'), NS(id=NS(union_id='on_other'), name='Bot')):
            assert message_router._is_bot_mentioned([mention])
        assert not message_router._is_bot_mentioned([NS(id=NS(union_id='on_other'), name='Other')])
        os.environ.pop('TEST_BOT_NAME')
        assert message_router._bot_identity(required=True) == ('on_bot', '')

    def test_missing_empty_and_whitespace_never_match(self) -> None:
        self.configure(robotUnionIdEnv='TEST_BOT_ID', robotNameEnv='TEST_BOT_NAME')
        for value in (None, '', ' \t '):
            if value is not None:
                os.environ.update(TEST_BOT_ID=value, TEST_BOT_NAME=value)
            for mentions in (None, [], [NS()], [NS(id=None, name=None)], [NS(id=NS(union_id=None), name='')]):
                assert not message_router._is_bot_mentioned(mentions)
            with pytest.raises(RuntimeError, match='must be non-empty'):
                message_router._bot_identity(required=True)

    async def test_startup_rejects_missing_identity_before_connecting(self) -> None:
        self.configure(robotUnionIdEnv='TEST_BOT_ID')
        with patch.object(self.g, 'create_websocket_client') as connect:
            with pytest.raises(RuntimeError, match='must be non-empty'):
                await self.g.main()
        connect.assert_not_called()

    async def test_group_without_bot_mention_is_ignored(self) -> None:
        self.configure(robotUnionIdEnv='TEST_BOT_ID')
        os.environ['TEST_BOT_ID'] = 'on_bot'
        data = helpers.event('hello')
        data.event.message.chat_type = 'group'
        data.event.message.mentions = [NS(id=NS(union_id='on_other'))]
        self.router._route_message = AsyncMock()
        await self.router.processing(data)
        self.router._route_message.assert_not_awaited()
        data.event.message.mentions = [NS(id=NS(union_id='on_bot'))]
        await self.router.processing(data)
        self.router._route_message.assert_awaited_once()

    def test_console_entry_runs_async_main(self) -> None:
        self.g.main = AsyncMock()
        self.g.cli()
        self.g.main.assert_awaited_once()
