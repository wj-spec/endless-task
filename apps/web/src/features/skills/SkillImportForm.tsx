import { useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import type { SkillImportResult, SkillValidation } from "../chat/apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";

export type SkillImportFormProps = {
  workspaceId: string;
  onDone: () => void;
  onCancel: () => void;
};

/**
 * S2 导入技能：先校验（诊断 + 扫描）再导入；同名不同内容需显式确认升级。
 */
export function SkillImportForm({
  workspaceId,
  onDone,
  onCancel,
}: SkillImportFormProps) {
  const [sourcePath, setSourcePath] = useState("");
  const [scope, setScope] = useState<"user" | "workspace">(
    workspaceId ? "workspace" : "user",
  );
  const [validation, setValidation] = useState<SkillValidation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmUpgrade, setConfirmUpgrade] = useState(false);

  const run = async (allowUpgrade: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const result: SkillImportResult = await chatApi.importSkill({
        sourcePath: sourcePath.trim(),
        scope,
        workspaceId: scope === "workspace" ? workspaceId : null,
        allowUpgrade,
      });
      setValidation(null);
      onDone();
      setSourcePath("");
      setError(
        result.upgraded
          ? `已升级 ${result.name}（旧版本已归档，可回滚）。`
          : `已导入 ${result.name}@${result.version}。`,
      );
    } catch (cause: unknown) {
      const message = readableError(cause) || "导入失败。";
      setError(message);
      if (message.includes("升级")) setConfirmUpgrade(true);
    } finally {
      setBusy(false);
    }
  };

  const validate = async () => {
    if (!sourcePath.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setValidation(await chatApi.validateSkill({ path: sourcePath.trim() }));
    } catch (cause: unknown) {
      setValidation(null);
      setError(readableError(cause) || "校验失败。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="导入技能" className="skill-workbench-form">
      <div className="skill-form-row">
        <input
          aria-label="技能目录路径"
          onChange={(event) => setSourcePath(event.target.value)}
          placeholder="本地技能目录（含 SKILL.md）"
          value={sourcePath}
        />
        <select
          aria-label="导入到"
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
      {validation ? (
        <div
          className={
            validation.valid
              ? "skill-validation is-ok"
              : "skill-validation is-bad"
          }
        >
          <p>
            {validation.manifest.name}@{validation.manifest.version} ·{" "}
            {validation.valid ? "校验通过" : "校验未通过"}
            {validation.scan.worstLevel
              ? ` · 风险 ${validation.scan.worstLevel}`
              : ""}
          </p>
          {validation.diagnostics.map((item) => (
            <p key={item.code}>· {item.message}</p>
          ))}
          {validation.scan.findings.map((item) => (
            <p key={`${item.code}-${item.path}`}>
              · [{item.level}] {item.message}
            </p>
          ))}
        </div>
      ) : null}
      {error ? (
        <p className="proposal-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="skill-form-actions">
        <button disabled={busy || !sourcePath.trim()} onClick={() => void validate()} type="button">
          校验
        </button>
        <button
          disabled={busy || !sourcePath.trim()}
          onClick={() => void run(false)}
          type="button"
        >
          {busy ? "处理中…" : "导入"}
        </button>
        <button onClick={onCancel} type="button">
          取消
        </button>
      </div>
      {confirmUpgrade ? (
        <ConfirmDialog
          body="同名技能已存在且内容不同。继续会归档旧版本并激活新版本，可手工回滚。"
          confirmLabel="确认升级"
          onClose={() => setConfirmUpgrade(false)}
          onConfirm={() => {
            setConfirmUpgrade(false);
            void run(true);
          }}
          title="升级已存在的技能？"
        />
      ) : null}
    </section>
  );
}
