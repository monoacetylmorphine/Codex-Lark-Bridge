#!/bin/sh
# 在 SSH 目标主机执行；仅依赖 Python 3 标准库和已配置的 Docker CLI。
set -eu
command -v python3 >/dev/null 2>&1 || { echo '缺少 python3' >&2; exit 1; }
export DEPLOY_SCRIPT_DIR
DEPLOY_SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 - "$@" <<'PY'
"""单机发布事务：预检、停机备份、更新、就绪验证和显式兼容回退。"""
import datetime
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

SERVICES = ('codex_bridge', 'mcp_service')
# 来源：单机镜像拉取/启动预算；不代表业务请求超时。
COMMAND_TIMEOUT = 600
READY_TIMEOUT = 180


def run(args, env=None, capture=False, timeout=COMMAND_TIMEOUT):
    return subprocess.run(args, env=env, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, timeout=timeout).stdout


def read_json(path):
    return json.loads(path.read_text())


def image_refs(manifest):
    if not re.fullmatch(r'[0-9a-f]{40}', manifest.get('revision', '')):
        raise ValueError('发布 revision 必须为完整 commit SHA')
    if manifest.get('platform') != 'linux/arm64':
        raise ValueError('目标只接受 linux/arm64 镜像')
    refs = manifest['images']
    if set(refs) != set(SERVICES):
        raise ValueError('发布必须包含两个应用镜像')
    for ref in refs.values():
        if not re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}', ref):
            raise ValueError('镜像必须是 GHCR 的完整 digest 引用')
    return refs


def main():
    if not os.environ.get('DEPLOY_ROOT'):
        raise ValueError('缺少环境变量 DEPLOY_ROOT')
    root = Path(os.environ['DEPLOY_ROOT'])
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('DEPLOY_ROOT 必须是已准备好的绝对目录')
    root = root.resolve()
    bundle = Path(os.environ['DEPLOY_SCRIPT_DIR']).resolve()
    manifest = read_json(bundle / 'release.json')
    refs = image_refs(manifest)
    host = read_json(root / 'host.json')
    project = host['project']
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', project):
        raise ValueError('无效的 Compose 项目名')
    directories = {}
    for key in ('host_codex_dir', 'host_dir'):
        path = Path(host[key])
        if not path.is_absolute() or not path.is_dir() or ',' in str(path):
            raise ValueError('挂载目录必须存在、为绝对路径且不含逗号：' + key)
        directories[key] = path.resolve()
    if type(host['mcp_port']) is not int or not 1 <= host['mcp_port'] <= 65535:
        raise ValueError('mcp_port 必须为有效端口')
    data = directories['host_dir']
    for name in ('config.json', '.env'):
        if not (data / name).is_file():
            raise ValueError('缺少运行配置文件：' + name)
    config = read_json(data / 'config.json')
    db = str(config['storage']['databasePath'])
    if db.startswith('~/'):
        db = '/home/app/' + db[2:]
    db_path = PurePosixPath(db)
    if not db_path.is_absolute():
        db_path = PurePosixPath('/home/app/.codex_bridge') / db_path
    relative = db_path.relative_to('/home/app/.codex_bridge')
    database = (data / str(relative)).resolve()
    if not database.is_relative_to(data):
        raise ValueError('数据库必须位于 .codex_bridge 挂载中，便于一致性备份')
    if config['mcp']['transport'] != 'streamable-http':
        raise ValueError('生产部署要求 MCP streamable-http')

    environment = os.environ.copy()
    environment.update(HOST_CODEX_DIR=str(directories['host_codex_dir']),
                       HOST_DIR=str(data), MCP_PORT=str(host['mcp_port']))

    def compose(directory, release, *args):
        env = environment.copy()
        images = image_refs(release)
        env.update(CODEX_IMAGE=images['codex_bridge'], MCP_IMAGE=images['mcp_service'])
        return run(['docker', 'compose', '--env-file', '/dev/null', '-p', project,
                    '-f', str(directory / 'compose.production.yaml'), *args], env=env)

    def containers(service, all_containers=False):
        return run(['docker', 'ps', '-aq' if all_containers else '-q', '--filter',
                    'label=com.docker.compose.service=' + service], capture=True).split()

    def inspect(identifier):
        return json.loads(run(['docker', 'inspect', identifier], capture=True))[0]

    current_path = root / 'current.json'
    # 先获取跨 SSH 会话锁。中断遗留锁须人工核查，不自动抢占。
    lock = root / '.deploy-lock'
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        previous = read_json(current_path) if current_path.exists() else None
        if previous and previous['host_hash'] != hashlib.sha256((root / 'host.json').read_bytes()).hexdigest():
            raise ValueError('host.json 已变化，需先确认挂载和端口迁移，禁止自动更新')
        previous_bundle = Path(previous['bundle']) if previous else None
        if previous:
            image_refs(previous['release'])
            if not previous_bundle.is_relative_to(root) or not previous_bundle.is_dir():
                raise ValueError('旧发布目录不存在或不属于 DEPLOY_ROOT')
        backend = run(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'], capture=True).strip()
        if backend not in ('linux/aarch64', 'linux/arm64'):
            raise ValueError('需要 Apple Silicon 对应的 Linux arm64 Docker 环境')
        run(['docker', 'network', 'inspect', 'ai-infra'], capture=True)
        existing = []
        for service in SERVICES:
            for identifier in containers(service, all_containers=True):
                container = inspect(identifier)
                owner = container['Config']['Labels']['com.docker.compose.project']
                if owner == project:
                    if previous is None:
                        raise ValueError('目标项目已有容器但没有发布记录；禁止自动接管')
                    existing.append(identifier)
                elif container['State']['Running']:
                    raise ValueError('其他 Compose 项目的应用仍运行，请先完成首次切换')
        compose(bundle, manifest, 'config', '--quiet')
        compose(bundle, manifest, 'pull', *SERVICES)
        for ref in refs.values():
            image = inspect(ref)
            if image.get('Architecture') != 'arm64' or image.get('Os') != 'linux':
                raise ValueError('镜像架构不匹配')
            if image['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != manifest['revision']:
                raise ValueError('镜像 revision 与发布记录不匹配')
        # 不使用入口初始化脚本；预检仅加载配置并验证必需飞书变量，不发送请求。
        preflight = (
            'import os; from codex_bridge.configs.loader import CONFIG; '
            'from codex_bridge.services.lark.lark_client import LARK_APP_ID; '
            'from codex_bridge.middleware.message_router import _bot_identity; '
            '_bot_identity(required=True); '
            'assert os.access("/home/app/.codex_bridge", os.W_OK); '
            'assert os.access("/home/app/.codex", os.W_OK)'
        )
        run(['docker', 'run', '--rm', '--entrypoint', 'python',
             '-e', 'CONFIG_FILE=/home/app/.codex_bridge/config.json',
             '--mount', 'type=bind,src=' + str(data) + ',dst=/home/app/.codex_bridge',
             '--mount', 'type=bind,src=' + str(directories['host_codex_dir']) + ',dst=/home/app/.codex',
             refs['codex_bridge'], '-c', preflight])

        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        record = root / ('release-' + stamp + '-' + uuid.uuid4().hex[:8])
        record.mkdir(mode=0o700)
        for filename in ('release.json', 'compose.production.yaml'):
            shutil.copy2(bundle / filename, record / filename)
        shutil.copy2(bundle / 'deploy.sh', record / 'deploy.sh')
        stopped = False
        changed = False
        try:
            if previous:
                # 标记在 stop 之前，以便部分停止或超时也进入恢复。
                stopped = True
                compose(previous_bundle, previous['release'], 'stop', *SERVICES)
                for identifier in existing:
                    state = inspect(identifier)['State']
                    if state['Running'] or state['ExitCode'] == 137:
                        raise RuntimeError('旧容器未完成正常退出，停止发布并恢复旧版本')
            shutil.copy2(data / 'config.json', record / 'config.backup.json')
            if database.exists():
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
                    with closing(sqlite3.connect(record / 'state.backup.sqlite')) as target:
                        source.backup(target)
            changed = True
            compose(record, manifest, 'up', '-d', '--no-build', '--pull', 'never',
                    '--wait', '--wait-timeout', str(READY_TIMEOUT), *SERVICES)
            receipt = {'bundle': str(record), 'release': manifest,
                       'host_hash': hashlib.sha256((root / 'host.json').read_bytes()).hexdigest()}
            pending = root / 'current.pending.json'
            pending.write_text(json.dumps(receipt, indent=2) + '\n')
            pending.replace(current_path)
        except BaseException:
            if not changed and stopped:
                compose(previous_bundle, previous['release'], 'start', *SERVICES)
            elif changed:
                compose(record, manifest, 'stop', *SERVICES)
                if previous and os.environ.get('ROLLBACK_COMPATIBLE') == 'true':
                    compose(previous_bundle, previous['release'], 'up', '-d', '--no-build', '--pull', 'never',
                            '--wait', '--wait-timeout', str(READY_TIMEOUT), *SERVICES)
                    print('部署失败，已验证旧镜像恢复；未恢复或覆盖数据库。', file=sys.stderr)
                else:
                    print('部署失败，应用已停止；未授权兼容回退或没有旧发布，请人工处理。', file=sys.stderr)
            raise
        print('部署完成：' + manifest['revision'])
    finally:
        lock.unlink()


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except Exception as exc:
        print('部署失败：' + str(exc), file=sys.stderr)
        sys.exit(1)
PY
