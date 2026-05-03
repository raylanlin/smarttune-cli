"""smarttune.platform — 多平台适配层。"""

from smarttune.platform.base import PlatformAdapter
from smarttune.platform.registry import (
    register,
    get_adapter,
    detect_platform,
    resolve_adapter,
    list_platforms,
)

__all__ = [
    "PlatformAdapter",
    "register",
    "get_adapter",
    "detect_platform",
    "resolve_adapter",
    "list_platforms",
]
