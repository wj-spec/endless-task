import { useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";

export type SkillCreateFormProps = {
  workspaceId: string;
  onDone: () => void;
  onCancel: () => void;
};

/** S2 新建技能：按 v2 模板写入技能根（用户级或当前工作区）。 */
export function SkillCreateForm({
  workspaceId,
  onDone,
  onCancel,
}: SkillCreateFormProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [whenToUse, setWhenToUse] = useState("");
  const [body, setBody] = useState("");
  const [scope, setScope] = useState<"user" | "workspace">(
    workspaceId ? "workspace" : "user",
  );
  const [userInvocable, setUserInvocable] = useState(true);
  const [modelInvocable, setModelInvocable] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await chatApi.createSkill({
        name: name.trim(),
        description: description.trim(),
        whenToUse: whenToUse.trim() || undefined,
        body: body.trim(),
        scope,
        workspaceId: scope === "workspace" ? workspaceId : null,
        modelInvocable,
        userInvocable,
      });
      setCreated(result.path);
      onDone();
    } catch (cause: unknown) {
      setError(readableError(cause) || "创建失败。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="新建技能" className="skill-workbench-form">
      <div className="skill-form-row">
        <input
          aria-label="技能名"
          onChange={(event) => setName(event.target.value)}
          placeholder="技能名（小写字母、数字、连字符）"
          value={name}
        />
        <select
          aria-label="创建到"
          onChange={(event) =>
            setScope(event.target.value as "user" | "workspace")
          }
          value={scope}
        >
          <option value="user">用户级</option>
          <option disabled={!workspaceId} value="workspace">
            当前工作区
          </option>
        </select>
      </div>
      <input
        aria-label="技能描述"
        onChange={(event) => setDescription(event.target.value)}
        placeholder="描述（模型据此判断是否使用）"
        value={description}
      />
      <input
        aria-label="适用场景"
        onChange={(event) => setWhenToUse(event.target.value)}
        placeholder="whenToUse（可选：什么情况下用）"
        value={whenToUse}
      />
      <textarea
        aria-label="技能正文"
        onChange={(event) => setBody(event.target.value)}
        placeholder="正文：步骤、约束、示例"
        rows={5}
        value={body}
      />
      <div className="skill-form-row">
        <label>
          <input
            checked={modelInvocable}
            onChange={(event) => setModelInvocable(event.target.checked)}
            type="checkbox"
          />
          允许模型自动使用
        </label>
        <label>
          <input
            checked={userInvocable}
            onChange={(event) => setUserInvocable(event.target.checked)}
            type="checkbox"
          />
          允许 /技能名 显式调用
        </label>
      </div>
      {error ? (
        <p className="proposal-error" role="alert">
          {error}
        </p>
      ) : null}
      {created ? <p className="file-tree-note">已创建：{created}</p> : null}
      <div className="skill-form-actions">
        <button
          disabled={busy || !name.trim() || !description.trim() || !body.trim()}
          onClick={() => void submit()}
          type="button"
        >
          {busy ? "创建中…" : "创建"}
        </button>
        <button onClick={onCancel} type="button">
          取消
        </button>
      </div>
    </section>
  );
}
