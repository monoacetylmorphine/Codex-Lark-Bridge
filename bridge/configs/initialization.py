"""Atomically initialize shared configuration in the mounted volume; both containers may start concurrently."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def initialize(default_file: str | Path, target: str | Path) -> None:
    """仅在目标配置不存在时复制默认文件，以硬链接原子发布完整内容。

    并发创建目标时保留先成功的文件；finally 删除本次临时文件，其余文件系统异常向外传播。
    """
    target = Path(target)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".config-", delete=False) as file:
            temporary = Path(file.name)
            file.write(Path(default_file).read_bytes())
            file.flush()
            os.fsync(file.fileno())
        try:
            # Publish only complete files using hard links; never overwrite an existing target.
            os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    """初始化默认位置的共享配置；显式指定其他配置路径时只验证文件存在，不自动创建。"""
    default_target = Path.home() / ".codex_bridge/config.json"
    target = Path(os.getenv("CONFIG_FILE", str(default_target))).expanduser()
    if target == default_target:
        initialize(Path(__file__).with_name("config_default.json"), target)
    elif not target.is_file():
        raise RuntimeError(f"The specified configuration file does not exist: {target}")


if __name__ == "__main__":
    main()
