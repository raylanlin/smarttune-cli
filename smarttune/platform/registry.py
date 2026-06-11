"""
smarttune/platform/registry.py

平台注册表 — 管理所有已注册的平台适配器，提供自动检测。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Type

from smarttune.platform.base import PlatformAdapter
from smarttune.errors import UnsupportedPlatformError, LogFormatError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局注册表
# ---------------------------------------------------------------------------

_registry: Dict[str, Type[PlatformAdapter]] = {}

# 注册期捕获的元数据快照（A3 修复：list_platforms 不再每次调用都
# 重新实例化全部适配器；适配器构造函数应保持无副作用，因为
# register() 仍需实例化一次以读取 property 形式的 name）
_metadata: Dict[str, Dict[str, str]] = {}


def register(adapter_cls: Type[PlatformAdapter]) -> Type[PlatformAdapter]:
    """注册一个平台适配器类。

    可用作类装饰器:

        @register
        class ArduPilotAdapter(PlatformAdapter):
            ...

    Parameters
    ----------
    adapter_cls : Type[PlatformAdapter]
        适配器类（不是实例）

    Returns
    -------
    Type[PlatformAdapter]
        原样返回，便于做装饰器
    """
    # 实例化一次获取 name 及元数据快照（仅注册期这一次）
    instance = adapter_cls()
    name = instance.name
    if name in _registry:
        logger.warning("Platform %r already registered, overwriting", name)
    _registry[name] = adapter_cls
    _metadata[name] = {
        "name": instance.name,
        "display_name": instance.display_name,
        "extensions": ", ".join(instance.supported_extensions),
        "capabilities": ", ".join(sorted(instance.capabilities())),
    }
    logger.debug("Registered platform adapter: %s (%s)", name, adapter_cls.__name__)
    return adapter_cls


def get_adapter(name: str) -> PlatformAdapter:
    """按名称获取平台适配器实例。

    Parameters
    ----------
    name : str
        平台名称: "ardupilot", "betaflight", "px4"

    Returns
    -------
    PlatformAdapter
        适配器实例

    Raises
    ------
    UnsupportedPlatformError
        平台未注册
    """
    name_lower = name.lower()
    if name_lower not in _registry:
        available = ", ".join(sorted(_registry.keys())) or "(none)"
        raise UnsupportedPlatformError(
            message=f"Unsupported platform: {name!r}",
            hint=f"Available platforms: {available}",
        )
    return _registry[name_lower]()


def detect_platform(path: Path) -> PlatformAdapter:
    """自动检测日志文件所属平台。

    遍历所有已注册的适配器，调用 detect() 方法。
    第一个返回 True 的适配器胜出。

    Parameters
    ----------
    path : Path
        日志文件路径

    Returns
    -------
    PlatformAdapter
        匹配的适配器实例

    Raises
    ------
    LogFormatError
        没有适配器能识别该文件
    """
    for name, adapter_cls in _registry.items():
        try:
            if adapter_cls.detect(path):
                logger.info("Auto-detected platform: %s for %s", name, path.name)
                return adapter_cls()
        except Exception as exc:
            logger.debug("Detection failed for %s: %s", name, exc)
            continue

    available = ", ".join(sorted(_registry.keys())) or "(none)"
    raise LogFormatError(
        message=f"Cannot identify log format: {path.name}",
        hint=(
            f"Supported platforms: {available}\n"
            "Use --platform to specify manually, or check if the file is a valid flight log."
        ),
    )


def list_platforms() -> List[Dict[str, str]]:
    """列出所有已注册的平台。

    Returns
    -------
    List[Dict[str, str]]
        每个元素: {"name": ..., "display_name": ..., "extensions": ..., "capabilities": ...}
    """
    # A3 修复：返回注册期快照，不再逐个重新实例化适配器
    return [dict(_metadata[name]) for name in sorted(_metadata.keys())]


def resolve_adapter(platform: str, log_path: Path) -> PlatformAdapter:
    """解析平台参数 — 统一入口。

    Parameters
    ----------
    platform : str
        "auto" 或具体平台名
    log_path : Path
        日志文件路径

    Returns
    -------
    PlatformAdapter
        适配器实例
    """
    if platform == "auto":
        return detect_platform(log_path)
    return get_adapter(platform)


# ---------------------------------------------------------------------------
# 自动发现并注册内置适配器
# ---------------------------------------------------------------------------

def _auto_discover():
    """导入所有内置平台模块，触发 @register 装饰器。"""
    # 延迟导入，避免循环依赖
    try:
        import smarttune.platform.ardupilot  # noqa: F401
    except ImportError:
        logger.debug("ArduPilot adapter not available")
    try:
        import smarttune.platform.betaflight  # noqa: F401
    except ImportError:
        logger.debug("Betaflight adapter not available")
    try:
        import smarttune.platform.px4  # noqa: F401
    except ImportError:
        logger.debug("PX4 adapter not available")


_auto_discover()
