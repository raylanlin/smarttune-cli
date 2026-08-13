"""smarttune.platform — 多平台适配层。

导入代价说明（v3.2）
--------------------
``PlatformAdapter`` / registry 会通过 ``models.flight_data`` 拉起 numpy。
参数查询类工具（``ParamTable``、``param_lint``）本身只用标准库，但过去
``from smarttune.platform.params import ParamTable`` 会先执行本文件、
把整套科学计算栈一起加载 —— MCP 每次 call_tool 都新起进程时，查一个参数
也要付 numpy 的启动成本。

因此这里改用 PEP 562 的模块级 ``__getattr__`` 延迟导入：公开 API 不变，
但只有真正取用适配器时才加载 numpy 链路。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 类型检查器仍看到真实符号
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

_LAZY = {
    "PlatformAdapter": ("smarttune.platform.base", "PlatformAdapter"),
    "register": ("smarttune.platform.registry", "register"),
    "get_adapter": ("smarttune.platform.registry", "get_adapter"),
    "detect_platform": ("smarttune.platform.registry", "detect_platform"),
    "resolve_adapter": ("smarttune.platform.registry", "resolve_adapter"),
    "list_platforms": ("smarttune.platform.registry", "list_platforms"),
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value  # 只解析一次
    return value


def __dir__():
    return sorted(__all__)
