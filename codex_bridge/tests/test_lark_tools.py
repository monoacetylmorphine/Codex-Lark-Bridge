"""Real SDK request models and resource validation against an offline client."""
from __future__ import annotations
import pytest
import importlib.util, pytest
import asyncio, pytest
import io, pytest
from pathlib import Path
import sys, pytest
import tempfile, pytest
import threading, pytest
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import Mock, patch
from codex_bridge.configs.loader import CONFIG
from codex_bridge.middleware.resource_validator import ResourceValidationError

class LarkToolsTests:

    def setup_method(self) -> None:
        self.directory = Path(self._pytest_stack.enter_context(tempfile.TemporaryDirectory()))
        self._pytest_stack.enter_context(patch.dict(CONFIG['storage'], workspaceRoot=str(self.directory)))
        self.client = NS(im=NS(v1=NS(message_resource=NS(get=Mock()), message_reaction=NS(create=Mock()), message=NS(list=Mock()))))
        stub = ModuleType('codex_bridge.services.lark.lark_client')
        stub.client = self.client
        spec = importlib.util.spec_from_file_location('lark_tools_offline_tests', Path(__file__).resolve().parents[1] / 'services/lark/lark_tools.py')
        self.tools = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {stub.__name__: stub, spec.name: self.tools}):
            spec.loader.exec_module(self.tools)
        self._pytest_stack.enter_context(patch.object(self.tools.lark, 'logger'))

    def response(self, success=True, **fields):
        return NS(success=lambda: success, code=999, msg='denied', get_log_id=lambda: 'log', raw=NS(content=b'{}', headers={}), data=None, **fields)

    async def test_download_validates_renames_and_writes_original_bytes(self) -> None:
        payload = b'\x89PNG\r\n\x1a\nimage'
        response = self.response(file=io.BytesIO(payload), file_name='../../photo-é.exe')
        response.raw.headers = {'Content-Type': 'image/png', 'Content-Length': len(payload)}
        self.client.im.v1.message_resource.get.return_value = response
        result = await self.tools.download_msg_resource('user', 'message', 'image-key', 'image')
        path = Path(result)
        assert path.parent == self.directory / 'user' / CONFIG['storage']['inboundSubdirectory']
        assert path.name.startswith('photo-é_')
        assert path.suffix == '.png'
        assert path.read_bytes() == payload
        request = self.client.im.v1.message_resource.get.call_args.args[0]
        assert (request.message_id, request.file_key) == ('message', 'image-key')
        assert ('type', 'image') in request.queries

    async def test_invalid_resource_is_never_written(self) -> None:
        self.client.im.v1.message_resource.get.return_value = self.response(file=io.BytesIO(b'not png'), file_name='fake.png')
        with pytest.raises(ResourceValidationError):
            await self.tools.download_msg_resource('user', 'message', 'key', 'image')
        assert [p for p in self.directory.rglob('*') if p.is_file()] == []

    async def test_slow_large_write_does_not_block_event_loop(self) -> None:
        entered, release = (threading.Event(), threading.Event())

        def slow_write(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Test write was not released')
            return 'written'
        self.client.im.v1.message_resource.get.return_value = self.response()
        with patch.object(self.tools, '_save_resource', side_effect=slow_write):
            task = asyncio.create_task(self.tools.download_msg_resource('user', 'message', 'key', 'file'))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                await asyncio.wait_for(asyncio.sleep(0.01), 0.2)
                assert not task.done()
            finally:
                release.set()
                result = await task
        assert result == 'written'

    async def test_failed_resource_api_does_not_create_destination(self) -> None:
        self.client.im.v1.message_resource.get.return_value = self.response(False)
        assert await self.tools.download_msg_resource('user', 'message', 'key', 'file') is None
        assert list(self.directory.iterdir()) == []

    async def test_resource_transport_exception_is_not_reported_as_success(self) -> None:
        self.client.im.v1.message_resource.get.side_effect = OSError('offline')
        with pytest.raises(OSError, match='offline'):
            await self.tools.download_msg_resource('user', 'message', 'key', 'file')
        assert list(self.directory.iterdir()) == []

    async def test_history_requests_descending_order_and_page_size(self) -> None:
        response = self.response()
        response.data = NS(items=[NS(message_id='new'), NS(message_id='old')])
        self.client.im.v1.message.list.return_value = response
        result = await self.tools.getting_chat_history('chat', 15)
        assert result == response.data.items
        assert result is not response.data.items
        query = dict(self.client.im.v1.message.list.call_args.args[0].queries)
        assert query['container_id'] == 'chat'
        assert query['container_id_type'] == 'chat'
        assert query['sort_type'] == 'ByCreateTimeDesc'
        assert query['page_size'] == '15'

    async def test_empty_and_failed_history_return_empty_list(self) -> None:
        for success, items in ((True, None), (True, []), (False, None)):
            response = self.response(success)
            response.data = NS(items=items)
            self.client.im.v1.message.list.return_value = response
            assert await self.tools.getting_chat_history('chat', 10) == []

    async def test_add_reaction_returns_id_only_on_success(self) -> None:
        for success in (True, False):
            response = self.response(success)
            response.data = NS(reaction_id='reaction')
            self.client.im.v1.message_reaction.create.return_value = response
            with patch.object(self.tools.lark.JSON, 'marshal', return_value='{}'):
                result = await self.tools.adding_reaction_emoji('message')
            assert result == ('reaction' if success else None)
            request = self.client.im.v1.message_reaction.create.call_args.args[0]
            assert request.message_id == 'message'
            assert request.request_body.reaction_type.emoji_type == CONFIG['lark']['reaction']['processingEmoji']
