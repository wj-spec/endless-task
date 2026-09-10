"""tasks 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。

搬家而非重构：每个 handler 仍闭包在 `container` 上，行为与搬家前一致。
见 docs/product-improvements/05-code-health/01-monolith-audit.md。
"""


from ..serialization import task_json, task_run_json
from endless_task.domain.models import TaskRunTrigger
from fastapi import FastAPI

from ..container import AppContainer


def register_tasks_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/tasks")
    async def list_tasks(include_cancelled: bool = False) -> dict[str, object]:
        tasks = container.task_repository.list_tasks(
            include_cancelled=include_cancelled
        )
        return {"items": [task_json(item) for item in tasks]}


    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, object]:
        return {"task": task_json(container.task_repository.get_task(task_id))}


    @app.post("/tasks/{task_id}/run", status_code=202)
    async def run_task(task_id: str) -> dict[str, object]:
        run = await container.task_worker.start(
            task_id, trigger=TaskRunTrigger.MANUAL
        )
        return {"run": task_run_json(run)}


    @app.post("/tasks/{task_id}/pause")
    async def pause_task(task_id: str) -> dict[str, object]:
        task = container.task_repository.pause_task(task_id)
        return {"task": task_json(task)}


    @app.post("/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> dict[str, object]:
        task = container.task_repository.resume_task(task_id)
        return {"task": task_json(task)}


    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, object]:
        task = container.task_repository.cancel_task(task_id)
        return {"task": task_json(task)}


    @app.get("/tasks/{task_id}/runs")
    async def list_task_runs(task_id: str) -> dict[str, object]:
        runs = container.task_run_repository.list_runs(task_id=task_id)
        return {"items": [task_run_json(item) for item in runs]}
