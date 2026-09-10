"""路由清单快照：巨石拆分期间的"行为不变"安全网。

`api/app.py` 的路由要逐个搬进 `api/routes/`。搬家是纯机械操作，风险集中在
"忘记注册 / 路径写错 / 前缀变化 / 重复注册"——这些在单个功能测试里不一定暴露，
但会在清单上立刻显形。

用法：
- 正常跑：断言当前 `(method, path)` 集合与冻结清单完全一致。
- 有意新增/删除路由时：`ENDLESS_TASK_UPDATE_ROUTE_SNAPSHOT=1 python -m unittest tests.test_api_route_inventory`
  重新生成 `tests/fixtures/api_routes.json`，并在提交里说明路由变化。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.routing import APIRoute

from endless_task.api import AppSettings, create_app

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "api_routes.json"

#: 只关心业务方法；HEAD/OPTIONS 由框架自动生成，不进快照。
TRACKED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def collect_routes(app) -> list[str]:
    entries: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if method in TRACKED_METHODS:
                entries.add(f"{method} {route.path}")
    return sorted(entries)


class ApiRouteInventoryTest(unittest.TestCase):
    def _routes(self) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(tmp) / "routes.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                    artifact_proposals_enabled=False,
                    task_proposals_enabled=False,
                    scheduler_enabled=False,
                    notifications_enabled=False,
                ),
            )
            return collect_routes(app)

    def test_route_inventory_matches_frozen_snapshot(self) -> None:
        routes = self._routes()
        if os.environ.get("ENDLESS_TASK_UPDATE_ROUTE_SNAPSHOT"):
            SNAPSHOT_PATH.write_text(
                json.dumps(routes, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.skipTest(f"route snapshot updated: {len(routes)} routes")
        self.assertTrue(
            SNAPSHOT_PATH.is_file(),
            f"缺少路由快照 {SNAPSHOT_PATH}；用 ENDLESS_TASK_UPDATE_ROUTE_SNAPSHOT=1 生成",
        )
        expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        missing = sorted(set(expected) - set(routes))
        added = sorted(set(routes) - set(expected))
        self.assertEqual(
            (missing, added),
            ([], []),
            "路由清单与快照不一致（拆分期间不允许变化）："
            f"\n  消失: {missing}\n  新增: {added}",
        )


if __name__ == "__main__":
    unittest.main()
