"""Real SDK request models and resource validation against an offline client."""

from __future__ import annotations

import importlib.util
import asyncio
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from codex_bridge.configs.loader import CONFIG
from codex_bridge.middleware.resource_validator import ResourceValidationError


class LarkToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(CONFIG['storage'], workspaceRoot=str(self.directory)))
        self.client = NS(im=NS(v1=NS(message_resource=NS(get=Mock()),
            message_reaction=NS(create=Mock()), message=NS(list=Mock()))))
        stub = ModuleType('codex_bridge.services.lark.lark_client')
        stub.client = self.client
        spec = importlib.util.spec_from_file_location('lark_tools_offline_tests',
            Path(__file__).resolve().parents[1] / 'services/lark/lark_tools.py')
        self.tools = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {stub.__name__: stub, spec.name: self.tools}):
            spec.loader.exec_module(self.tools)
        self.enterContext(patch.object(self.tools.lark, 'logger'))

    def response(self, success=True, **fields):
        return NS(success=lambda: success, code=999, msg='denied', get_log_id=lambda: 'log',
                  raw=NS(content=b'{}', headers={}), data=None, **fields)

    async def test_download_validates_renames_and_writes_original_bytes(self) -> None:
        payload = b'\x89PNG\r\n\x1a\nimage'
        response = self.response(file=io.BytesIO(payload), file_name='../../photo-é.exe')
        response.raw.headers = {'Content-Type': 'image/png', 'Content-Length': len(payload)}
        self.client.im.v1.message_resource.get.return_value = response
        result = await self.tools.download_msg_resource('user', 'message', 'image-key', 'image')
        path = Path(result)
        self.assertEqual(path.parent, self.directory / 'user' / CONFIG['storage']['inboundSubdirectory'])
        self.assertTrue(path.name.startswith('photo-é_'))
        self.assertEqual(path.suffix, '.png')
        self.assertEqual(path.read_bytes(), payload)
        request = self.client.im.v1.message_resource.get.call_args.args[0]
        self.assertEqual((request.message_id, request.file_key), ('message', 'image-key'))
        self.assertIn(('type', 'image'), request.queries)

    async def test_invalid_resource_is_never_written(self) -> None:
        self.client.im.v1.message_resource.get.return_value = self.response(file=io.BytesIO(b'not png'), file_name='fake.png')
        with self.assertRaises(ResourceValidationError):
            await self.tools.download_msg_resource('user', 'message', 'key', 'image')
        self.assertEqual([p for p in self.directory.rglob('*') if p.is_file()], [])

    async def test_slow_large_write_does_not_block_event_loop(self) -> None:
        entered, release = threading.Event(), threading.Event()
        def slow_write(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Test write was not released')
            return 'written'
        self.client.im.v1.message_resource.get.return_value = self.response()
        with patch.object(self.tools, '_save_resource', side_effect=slow_write):
            task = asyncio.create_task(self.tools.download_msg_resource('user', 'message', 'key', 'file'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.wait_for(asyncio.sleep(0.01), 0.2)
                self.assertFalse(task.done())
            finally:
                release.set()
                result = await task
        self.assertEqual(result, 'written')

    async def test_failed_resource_api_does_not_create_destination(self) -> None:
        self.client.im.v1.message_resource.get.return_value = self.response(False)
        self.assertIsNone(await self.tools.download_msg_resource('user', 'message', 'key', 'file'))
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_resource_transport_exception_is_not_reported_as_success(self) -> None:
        self.client.im.v1.message_resource.get.side_effect = OSError('offline')
        with self.assertRaisesRegex(OSError, 'offline'):
            await self.tools.download_msg_resource('user', 'message', 'key', 'file')
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_history_requests_descending_order_and_page_size(self) -> None:
        response = self.response()
        response.data = NS(items=[NS(message_id='new'), NS(message_id='old')])
        self.client.im.v1.message.list.return_value = response
        result = await self.tools.getting_chat_history('chat', 15)
        self.assertEqual(result, response.data.items)
        self.assertIsNot(result, response.data.items)
        query = dict(self.client.im.v1.message.list.call_args.args[0].queries)
        self.assertEqual(query['container_id'], 'chat')
        self.assertEqual(query['container_id_type'], 'chat')
        self.assertEqual(query['sort_type'], 'ByCreateTimeDesc')
        self.assertEqual(query['page_size'], '15')

    async def test_empty_and_failed_history_return_empty_list(self) -> None:
        for success, items in ((True, None), (True, []), (False, None)):
            with self.subTest(success=success, items=items):
                response = self.response(success)
                response.data = NS(items=items)
                self.client.im.v1.message.list.return_value = response
                self.assertEqual(await self.tools.getting_chat_history('chat', 10), [])

    async def test_add_reaction_returns_id_only_on_success(self) -> None:
        for success in (True, False):
            with self.subTest(success=success):
                response = self.response(success)
                response.data = NS(reaction_id='reaction')
                self.client.im.v1.message_reaction.create.return_value = response
                with patch.object(self.tools.lark.JSON, 'marshal', return_value='{}'):
                    result = await self.tools.adding_reaction_emoji('message')
                self.assertEqual(result, 'reaction' if success else None)
                request = self.client.im.v1.message_reaction.create.call_args.args[0]
                self.assertEqual(request.message_id, 'message')
                self.assertEqual(request.request_body.reaction_type.emoji_type, CONFIG['lark']['reaction']['processingEmoji'])
