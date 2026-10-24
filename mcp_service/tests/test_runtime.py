"""离线验证工具解耦，不读取真实凭据或发送请求。"""
import pytest
import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import subprocess
import socket
import tempfile
import threading
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class RuntimeTests:

    async def test_health_probe_over_real_local_http_without_credentials(self):
        from mcp_service.utils.health import probe
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        root = Path(__file__).resolve().parents[2]
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.temp.name, 'CONFIG_FILE': str(Path(self.temp.name) / 'config.json'), 'MCP_HOST': '127.0.0.1', 'MCP_PORT': str(port), 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen([sys.executable, '-B', '-m', 'mcp_service.server'], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with asyncio.timeout(15):
                while True:
                    if process.poll() is not None:
                        pytest.fail('Local MCP process exited before readiness')
                    try:
                        await probe(f'http://127.0.0.1:{port}/mcp')
                        break
                    except Exception:
                        await asyncio.sleep(0.1)
        finally:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)

    async def test_health_probe_only_lists_tools_and_rejects_missing_tools(self):
        from mcp_service.utils.health import probe
        from mcp_service.utils.health import REQUIRED_TOOLS
        client = AsyncMock()
        client.list_tools.return_value = type('Tools', (), {'tools': [type('Tool', (), {'name': name}) for name in REQUIRED_TOOLS]})()
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        with patch('mcp_service.utils.health.Client', return_value=manager):
            await probe('http://127.0.0.1:8000/mcp')
            client.call_tool.assert_not_called()
            client.list_tools.return_value.tools = []
            with pytest.raises(RuntimeError, match='not ready'):
                await probe('http://127.0.0.1:8000/mcp')

    @classmethod
    def setup_class(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='mcp-tests-')
        default = Path(__file__).resolve().parents[1] / 'configs/config_default.json'
        config = json.loads(default.read_text())
        config['mcp'].pop('imageModel', None)
        config_path = Path(cls.temp.name) / 'config.json'
        config_path.write_text(json.dumps(config))
        cls.env = patch.dict(os.environ, {'CONFIG_FILE': str(config_path)}, clear=True)
        cls.dotenv = patch('dotenv.load_dotenv')
        cls.env.start()
        cls.dotenv.start()
        cls.server = importlib.import_module('mcp_service.server')
        cls.image = importlib.import_module('mcp_service.tools.internal_tools.text2image')
        cls.sdk_loop = importlib.import_module('lark_oapi.ws.client').loop

    @classmethod
    def teardown_class(cls):
        cls.dotenv.stop()
        cls.env.stop()
        cls.temp.cleanup()
        if not cls.sdk_loop.is_running() and (not cls.sdk_loop.is_closed()):
            cls.sdk_loop.close()

    async def test_registration_without_any_credentials(self):
        tools = await self.server.mcp.list_tools()
        assert {tool.name for tool in tools} == {'sending_file', 'image_generator', 'commodity_sale_analysis', 'commodity_inventory_info'}

    async def test_shared_lark_requests_keep_service_dependencies(self):
        requests = importlib.import_module('mcp_service.services.lark.lark_requests')
        codex_requests = importlib.import_module('codex_bridge.services.lark.lark_requests')
        config = importlib.import_module('mcp_service.configs.loader')
        executor = importlib.import_module('mcp_service.utils.bounded_executor')
        assert Path(requests.__file__).resolve() == Path(codex_requests.__file__).resolve()
        assert requests.CONFIG is config.CONFIG
        assert isinstance(requests.executor, executor.BoundedExecutor)
        assert requests.CONFIG is not codex_requests.CONFIG
        assert requests.executor is not codex_requests.executor
        operation = MagicMock()
        with patch.object(requests.executor, 'call', new_callable=AsyncMock, return_value=42) as call:
            assert await requests.call_lark(operation, 'argument') == 42
        call.assert_awaited_once_with(operation, 'argument', timeout=config.CONFIG['codex']['watchdog']['cardRequestTimeoutSeconds'])

    async def test_missing_image_config_fails_only_image_call(self):
        with pytest.raises(TypeError, match='imageModel'):
            await self.image.image_generator('测试')
        assert len(await self.server.mcp.list_tools()) == 4

    async def test_image_call_does_not_need_lark_credentials_and_closes_client(self):
        model = {'apiKeyEnv': 'TEST_IMAGE_KEY', 'model': 'test-model', 'baseUrl': 'https://example.invalid'}
        client = MagicMock()
        client.images.generate = AsyncMock(return_value=MagicMock(data=[MagicMock(url='https://example.invalid/image.png')]))
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=client)
        manager.__aexit__ = AsyncMock(return_value=False)
        with patch.dict(self.image.CONFIG['mcp'], imageModel=model), patch.dict(os.environ, TEST_IMAGE_KEY='test-only'), patch.object(self.image, 'AsyncOpenAI', return_value=manager):
            result = await self.image.image_generator('测试')
            assert result == 'https://example.invalid/image.png'
        manager.__aexit__.assert_awaited_once()

    async def test_lark_call_validates_its_own_credentials(self):
        directory = Path(self.temp.name) / 'on_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        directory.mkdir(exist_ok=True)
        path = directory / 'file.txt'
        path.write_text('测试')
        with pytest.raises(RuntimeError, match='飞书配置'):
            await self.server.sending_file(str(path))

    async def test_invalid_paths_fail_before_client_or_upload(self):
        module = importlib.import_module('mcp_service.tools.lark_tools.sending_file')
        root = Path(self.temp.name)
        for relative in ('plain.txt', 'on_first/oc_second/file.txt', 'on_first.txt'):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('测试')
            with patch.object(module, 'get_client') as factory:
                with pytest.raises(ValueError):
                    await module.sending_file(str(path))
                factory.assert_not_called()
        with patch.object(module, 'get_client') as factory:
            for path in ('relative.txt', str(root / 'missing')):
                with pytest.raises((ValueError, FileNotFoundError)):
                    await module.sending_file(path)
            factory.assert_not_called()

    async def test_recipient_requires_regex_and_exact_length(self):
        module = importlib.import_module('mcp_service.tools.lark_tools.sending_file')
        for prefix in ('on_', 'oc_'):
            for length in (31, 33):
                path = Path(self.temp.name) / (prefix + 'a' * length) / 'file.txt'
                path.parent.mkdir(exist_ok=True)
                path.write_text('测试')
                with patch.object(module, 'get_client') as factory:
                    with pytest.raises(ValueError):
                        await module.sending_file(str(path))
                    factory.assert_not_called()
            valid = prefix + 'a' * 32
            assert module._recipient(Path('/tmp') / valid / 'file.txt') == valid
        for invalid in ('xx_' + 'a' * 32, 'on_' + '-' * 32):
            with pytest.raises(ValueError):
                module._recipient(Path('/tmp') / invalid / 'file.txt')
        with pytest.raises(ValueError):
            module._recipient(Path('/tmp') / ('on_' + 'a' * 32) / ('oc_' + 'b' * 32) / 'file.txt')

    async def test_configured_types_recipient_and_success_receipt(self):
        module = importlib.import_module('mcp_service.tools.lark_tools.sending_file')
        for recipient, kind in (('on_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'union_id'), ('oc_cccccccccccccccccccccccccccccccc', 'chat_id')):
            for suffix, expected in (('.PDF', 'pdf'), ('.txt', 'custom_stream')):
                path = Path(self.temp.name) / recipient / ('file' + suffix)
                path.parent.mkdir(exist_ok=True)
                path.write_text('测试内容')
                client = MagicMock()
                uploaded = MagicMock()
                uploaded.success.return_value = True
                uploaded.data.file_key = 'file-key'
                sent = MagicMock()
                sent.success.return_value = True
                sent.data.message_id = 'message-id'

                def upload(request):
                    assert request.request_body.file_type == expected
                    body = request.request_body.file.read()
                    expected_body = '测试内容'.encode()
                    assert body == expected_body
                    return uploaded
                client.im.v1.file.create.side_effect = upload
                client.im.v1.message.create.return_value = sent
                with patch.object(module, 'get_client', return_value=client), patch.dict(module.CONFIG['lark']['upload'], nativeFileTypes=['pdf'], fallbackFileType='custom_stream'):
                    result = await module.sending_file(str(path))
                assert result == {'recipient_id': recipient, 'file_key': 'file-key', 'message_id': 'message-id'}
                request = client.im.v1.message.create.call_args.args[0]
                assert request.receive_id_type == kind
                assert request.request_body.receive_id == recipient

    async def test_upload_and_send_failures_raise_without_retry(self):
        module = importlib.import_module('mcp_service.tools.lark_tools.sending_file')
        path = Path(self.temp.name) / 'on_dddddddddddddddddddddddddddddddd' / 'file.txt'
        path.parent.mkdir(exist_ok=True)
        path.write_text('测试')
        for stage in ('upload', 'send', 'missing_file_key', 'missing_message_id'):
            client = MagicMock()
            upload = client.im.v1.file.create.return_value
            upload.success.return_value = stage != 'upload'
            upload.data.file_key = None if stage == 'missing_file_key' else 'key'
            sent = client.im.v1.message.create.return_value
            sent.success.return_value = stage != 'send'
            sent.data.message_id = None if stage == 'missing_message_id' else 'id'
            with patch.object(module, 'get_client', return_value=client):
                with pytest.raises(RuntimeError):
                    await module.sending_file(str(path))
            client.im.v1.file.create.assert_called_once()
            if stage in ('upload', 'missing_file_key'):
                client.im.v1.message.create.assert_not_called()
            else:
                client.im.v1.message.create.assert_called_once()

    async def test_upload_timeout_does_not_close_worker_file_or_send_message(self):
        module = importlib.import_module('mcp_service.tools.lark_tools.sending_file')
        path = Path(self.temp.name) / ('on_' + 'e' * 32) / 'file.txt'
        path.parent.mkdir(exist_ok=True)
        path.write_text('测试')
        release, entered, exited = (threading.Event(), threading.Event(), threading.Event())
        files = []
        client = MagicMock()

        def upload(request):
            file = request.request_body.file
            files.append(file)
            entered.set()
            release.wait(2)
            try:
                return file.read()
            finally:
                exited.set()
        client.im.v1.file.create.side_effect = upload
        try:
            with patch.object(module, 'get_client', return_value=client), patch.dict(module.CONFIG['codex']['watchdog'], cardRequestTimeoutSeconds=0.03):
                task = asyncio.create_task(module.sending_file(str(path)))
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                with pytest.raises(TimeoutError):
                    await task
                assert not files[0].closed
                client.im.v1.message.create.assert_not_called()
        finally:
            release.set()
            while not exited.is_set():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
        assert files[0].closed

class ConfigScopeTests:

    def setup_method(self):
        root = Path(__file__).resolve().parents[1]
        self.config = json.loads((root / 'configs/config_default.json').read_text())
        spec = importlib.util.spec_from_file_location('mcp_service.configs.loader_under_test', root / 'configs/loader.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, CONFIG_FILE=str(root / 'configs/config_default.json')), patch('dotenv.load_dotenv'):
            spec.loader.exec_module(module)
        self.load_config = module._load_config

    def load(self, config):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(config))
            return self.load_config(path)

    def test_shared_codex_sections_are_required_and_validated(self):
        minimal = {key: self.config[key] for key in ('schemaVersion', 'mcp', 'lark', 'codex')}
        with pytest.raises(RuntimeError):
            self.load(minimal)
        for value in (-1, 0, 1.5, True, '10'):
            config = copy.deepcopy(self.config)
            config['codex']['watchdog']['maxRunSeconds'] = value
            with pytest.raises(RuntimeError):
                self.load(config)

    def test_own_timeout_upload_and_server_fields_are_validated(self):
        for keys, value in [(('codex', 'watchdog', 'cardRequestTimeoutSeconds'), float('nan')), (('lark', 'upload', 'nativeFileTypes'), 'pdf'), (('mcp', 'port'), 'bad')]:
            config = copy.deepcopy(self.config)
            parent = config
            for key in keys[:-1]:
                parent = parent[key]
            parent[keys[-1]] = value
            with pytest.raises(RuntimeError):
                self.load(config)

    def test_shared_loader_preserves_mcp_storage_paths(self):
        root = Path(__file__).resolve().parents[1]
        assert (root / 'configs/loader.py').resolve() == root.parent / 'codex_bridge/configs/loader.py'
        self.config['storage'].update(databasePath='state.sqlite', workspaceRoot='~/workspace')
        assert self.load(self.config)['storage'] == self.config['storage']

    def test_video_configuration_is_retained(self):
        loaded = self.load(self.config)
        assert loaded['mcp']['videoModel'] == self.config['mcp']['videoModel']
