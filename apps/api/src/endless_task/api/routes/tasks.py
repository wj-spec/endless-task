"""tasks 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。

搬家而非重构：每个 handler 仍闭包在 `container` 上，行为与搬家前一致。
"""


from ..serialization import task_json, task_run_json
from endless_task.domain.models import TaskRunTrigger
from endless_task.domain.repositories import NotFoundError, ValidationError
from fastapi import FastAPI

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.tasks import CreateTaskBody, UpdateTaskBody


def register_tasks_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/tasks")
    async def list_tasks(include_cancelled: bool = False) -> dict[str, object]:
        tasks = container.task_repository.list_tasks(
            include_cancelled=include_cancelled
        )
        return {"items": [task_json(item) for item in tasks]}


    @app.post("/tasks", status_code=201)
    async def create_task(body: CreateTaskBody) -> dict[str, object]:
        """显式创建一个定时任务（切片 10 T5）。

        **这不是第二份建任务实现**：本 handler 只做两件事——① 把缺字段翻成
        指名的 4xx（pydantic 的通用报错说不出是哪个字段缺）；② 把请求原样交给
        仓储唯一的缝 `SqliteTaskRepository.create_task`。title/commitment 的
        非空与长度、`schedule` 的频度/时间/越界语义，全部由那一处（及其调用的
        `parse_task_schedule`）裁决后指名冒错——这里不重写任何词表，也不做
        静默回落（提案抽取路径"provider 失败或答案 <40 字返回 ()"的毛病不复制；
        见 `tasks/proposal_service.py:107-113`）。

        校验一律发生在写库之前、且落库是单事务单行，所以非法输入**不留半行**。

        写出来的行与"提案接受"写出来的行同形（两边共用同一个
        `storage/sqlite_task_repository.py:insert_task_row`），因此后续触发与
        执行路径完全一致：due → `TaskScheduler.scan_once` → `TaskWorker.start`
        → `claim_run` → 该任务**自己的执行会话**（按 `(internal_owner_kind,
        internal_owner_id)` 查/建，见 `tasks/worker.py:_ensure_branch`）
        → `create_message_submission` → 真 v2 run → 终态通知。
        `source_conversation_id` 只作来源锚点：执行会话的环境（workspace /
        provider profile / model override）从它继承，执行内容**不**落进它。
        """
        for value, code, field in (
            (body.title, "missing_title", "title"),
            (body.commitment, "missing_commitment", "commitment"),
            (body.schedule, "missing_schedule", "schedule"),
            (
                body.conversationId,
                "missing_source_conversation",
                "conversationId",
            ),
            (body.turnId, "missing_source_turn", "turnId"),
        ):
            if value is None:
                raise ApiRequestError(
                    code,
                    f"{field} 是必填项。",
                    status_code=400,
                )
        # 切片 10 残留 A1：`turnId` 必须**确实指向该会话的一轮**。
        #
        # 两列（`source_conversation_id` / `source_turn_id`）在迁移 016 里是 NOT NULL，
        # 但此前没人校验二者**相容**：直接调 API 能建出"来源轮次属于**另一个**会话"的
        # 任务——`tasks.source_turn_id` 的语义被污染，而 worker 又以
        # `source_conversation_id` 作为执行会话的**环境来源**（
        # `tasks/worker.py:_ensure_branch`），于是"任务A的锚点指向会话B"会一路带到
        # 执行期。真实路径（提案抽取）里 `turn_id` **就是该轮的 v2 run id**
        # （`api/wiring.py` 的 `task_proposal_service.generate_for_turn(turn_id=run.id)`，
        # 前端 T6 也取 `runtimeSnapshot.activeRunId`），所以这里用**既有仓储读 run**、
        # 按同一条不变量校验：**零迁移、零新增路由**。
        #
        # 两种失败（该 run 不存在 / 该 run 属于别的会话）共用**同一个** code：既指名到
        # 输入字段，又不泄露"这个 run id 在别的会话里存在"——否则就成了一个跨会话的
        # 存在性探针。校验发生在 `create_task` **之前**、写库是单事务单行，所以不匹配
        # 的请求**不留任何库行**。
        try:
            # `or ""`：上面的必填校验已把 None 翻成 `missing_source_turn` 400，
            # 这里只是为了不把 `Optional[str]` 直接喂给仓储（空串绝不可能命中行）。
            source_run = container.runtime_v2_repository.get_run(body.turnId or "")
        except NotFoundError:
            source_run = None
        if source_run is None or source_run.conversation_id != body.conversationId:
            raise ApiRequestError(
                "source_turn_mismatch",
                "turnId 指向的对话轮次不存在，或不属于该 conversationId。",
                status_code=400,
            )
        task = container.task_repository.create_task(
            title=body.title,
            commitment=body.commitment,
            schedule=body.schedule,
            source_conversation_id=body.conversationId,
            source_turn_id=body.turnId,
        )
        return {"task": task_json(task)}


    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, object]:
        return {"task": task_json(container.task_repository.get_task(task_id))}


    @app.patch("/tasks/{task_id}")
    async def update_task(
        task_id: str, body: UpdateTaskBody
    ) -> dict[str, object]:
        """编辑一条定时任务（批次 D Task 1）。

        契约照 `PATCH /memories/{id}`：全 None → `ValidationError("Nothing to
        update.")`（400 `validation_error`）、逐字段交给仓储、响应
        `{"task": task_json(…)}`。

        允许面只有 `title` / `commitment` / `schedule`（R-D6）：`status` **不
        接受**（会与 `pause`/`resume`/`cancel` 的 `_transition` 形成双入口），
        `source_*` 与 `created_at` **不动**。`once` 依然不是任务（`schedule` 由
        `parse_task_schedule` 一处裁决），`cancelled` 是终态 → 409，不存在的
        task → 404。

        改 `schedule` 会让仓储在**同一个事务**里写 `resumed_at = now`，把触发
        anchor 重置到这一刻（R-D7）：`scan_once` 是无状态推导，不动 anchor 的话
        "改期到相对旧 anchor 已过去的时刻"会**立刻补跑一次**。只改
        title/commitment（或把当前排期原样读回写回）时 anchor 不动。
        """
        if (
            body.title is None
            and body.commitment is None
            and body.schedule is None
        ):
            raise ValidationError("Nothing to update.")
        task = container.task_repository.update_task(
            task_id,
            title=body.title,
            commitment=body.commitment,
            schedule=body.schedule,
        )
        return {"task": task_json(task)}


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


    @app.post("/tasks/{task_id}/runs/{run_id}/cancel")
    async def cancel_task_run(task_id: str, run_id: str) -> dict[str, object]:
        """取消一个**在飞**的定时任务/提醒 run（切片 10 T3 Step 1）。

        `POST /tasks/{task_id}/cancel`（上面那条）只改任务状态、碰不到正在跑的
        run；worker 以前用的是匿名 token，外部没有任何句柄。这条按 run id 寻址，
        走执行器既有的取消语义（终态收尾由 worker 落库）。

        语义必须**如实**：本进程没有这个 run 的在飞句柄（从未开始、已在别处终态、
        或不属于这个 task/提醒）时返回 `accepted: false`，绝不假装取消成功。
        路由同时覆盖提醒：`task_runs.task_id` 对提醒 run 存的就是 reminder id，
        `GET /tasks/{id}/runs` 一直是这样用的。
        """
        run = container.task_run_repository.get_run(run_id)
        if run.task_id != task_id:
            raise NotFoundError(f"Task run not found: {run_id}")
        accepted = container.task_worker.cancel(run_id)
        return {
            "runId": run_id,
            "accepted": accepted,
            "status": run.status.value,
        }
