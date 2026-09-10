"""v2 运行时仓储（聚合类）。

原先是一个 3 556 行、83 个方法的单类；按表族拆成
`storage/v2/{base,lanes,runs,transcript,tool_executions,events}.py` 后，这里只剩
组合声明。对外的方法面与签名保持不变（`tests/test_storage_method_inventory.py`
冻结），常量也从 base 重新导出，既有导入路径不受影响。
"""

from __future__ import annotations

from .v2.base import (  # noqa: F401  (re-export：既有调用方从这里取常量)
    ACTIVE_RUN_STATUSES,
    _ACTIVE_MODEL_TURN_STATUSES,
    _ACTIVE_TOOL_EXECUTION_STATUSES,
    V2RepositoryBase,
)
from .v2.events import EventRepositoryMixin
from .v2.lanes import LaneRepositoryMixin
from .v2.runs import RunRepositoryMixin
from .v2.tool_executions import ToolExecutionRepositoryMixin
from .v2.transcript import TranscriptRepositoryMixin


class SqliteRuntimeV2Repository(
    LaneRepositoryMixin,
    RunRepositoryMixin,
    TranscriptRepositoryMixin,
    ToolExecutionRepositoryMixin,
    EventRepositoryMixin,
):
    """SQLite persistence for the v2 Agent Runtime state model.

    The repository intentionally does not migrate v1 rows. M1 only provides the
    active v2 write path; the v1 tables remain untouched and readable.
    """
