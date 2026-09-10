"""`RuntimeV2SessionGateway`：三个 mixin 的组合，对外仍是同一个类。

`RuntimeV2SessionGateway` 原有 45 个方法、918 行，按职责拆成：
`base.py`（字段与只读视图）· `lanes.py`（泳道/记忆/临时会话）· `runs.py`
（发送/变体/恢复/审批/取消/运行管道）。**对外方法面与签名一字不变**，调用方零改动；
mixin 之间只允许"单向调用底座"，避免 MRO 上的循环依赖。
"""

from __future__ import annotations

from .base import _GatewayBase
from .lanes import _GatewayLanesMixin
from .runs import _GatewayRunsMixin


class RuntimeV2SessionGateway(_GatewayLanesMixin, _GatewayRunsMixin, _GatewayBase):
    """In-process Session Gateway for the v2 runtime."""


__all__ = ["RuntimeV2SessionGateway"]
