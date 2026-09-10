import { useState } from "react";
import { ApiClientError, chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import type { EcosystemSkillHit } from "../chat/apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";

export type SkillEcosystemFormProps = {
  workspaceId: string;
  onDone: () => void;
  onCancel: () => void;
};

type PendingInstall = {
  hit: EcosystemSkillHit;
  /** 同名不同内容 → 第二次确认后带 allowUpgrade 重试。 */
  upgrade: boolean;
};

/**
 * S8 从生态安装：检索 skills.sh → 选中 → 确认 → 服务端下载 + 扫描门禁 + 激活。
 *
 * 「安装」是显式用户动作（点击 + ConfirmDialog 双重确认）；模型侧走
 * `skill_install` 工具，按 `ENDLESS_TASK_SKILL_INSTALL` 决定是否需要审批。
 */
export function SkillEcosystemForm({
  workspaceId,
  onDone,
  onCancel,
}: SkillEcosystemFormProps) {
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<"user" | "workspace">(
    workspaceId ? "workspace" : "user",
  );
  const [hits, setHits] = useState<EcosystemSkillHit[] | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [busySpec, setBusySpec] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [pending, setPending] = useState<PendingInstall | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const search = async () => {
    const keyword = query.trim();
    if (!keyword) return;
    setSearching(true);
    setError(null);
    setNotice(null);
    try {
      const result = await chatApi.searchEcosystemSkills(keyword);
      setHits(result.items);
      setSearchError(result.error);
    } catch (cause: unknown) {
      setHits(null);
      setSearchError(null);
      setError(readableError(cause) || "检索失败，请稍后重试。");
    } finally {
      setSearching(false);
    }
  };

  const install = async (hit: EcosystemSkillHit, upgrade: boolean) => {
    setBusySpec(hit.spec);
    setError(null);
    setNotice(null);
    try {
      const outcome = await chatApi.installSkillFromEcosystem({
        source: hit.spec,
        scope,
        workspaceId: scope === "workspace" ? workspaceId : null,
        allowUpgrade: upgrade,
      });
      setPending(null);
      setNotice(
        `${outcome.skill.upgraded ? "已升级" : "已安装"} ${
          outcome.skill.name
        }@${outcome.skill.version} · ${
          outcome.skill.worstLevel
            ? `风险 ${outcome.skill.worstLevel}`
            : "扫描通过"
        }`,
      );
      onDone();
    } catch (cause: unknown) {
      if (cause instanceof ApiClientError && cause.code === "digest_conflict") {
        // 同名不同内容：升级需要用户再确认一次。
        setPending({ hit, upgrade: true });
      } else {
        setPending(null);
        setError(readableError(cause) || "安装失败。");
      }
    } finally {
      setBusySpec(null);
    }
  };

  return (
    <section aria-label="从生态安装技能" className="skill-workbench-form">
      <div className="skill-form-row">
        <input
          aria-label="生态检索关键词"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void search();
          }}
          placeholder="搜索生态技能，如 video podcast / resume ATS"
          value={query}
        />
        <select
          aria-label="安装到"
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
        <button
          disabled={searching || !query.trim()}
          onClick={() => void search()}
          type="button"
        >
          {searching ? "检索中…" : "检索生态"}
        </button>
      </div>
      <p className="skill-ecosystem-hint">
        来源
        skills.sh。安装会在服务端下载并经过静态扫描门禁（高危/严重直接拒绝），
        记录来源与摘要，可回滚。
      </p>
      {searchError ? (
        <p className="skill-validation is-bad">{searchError}</p>
      ) : null}
      {hits !== null && hits.length === 0 && !searchError ? (
        <p className="skill-validation">没有匹配的技能，换个关键词试试。</p>
      ) : null}
      {hits !== null && hits.length > 0 ? (
        <ul className="skill-ecosystem-list">
          {hits.map((hit) => (
            <li key={hit.spec}>
              <div className="skill-ecosystem-main">
                <code>{hit.spec}</code>
                <span className="knowledge-badge">{hit.installs} installs</span>
                <a href={hit.url} rel="noreferrer" target="_blank">
                  详情
                </a>
              </div>
              <button
                disabled={busySpec !== null}
                onClick={() => setPending({ hit, upgrade: false })}
                type="button"
              >
                {busySpec === hit.spec && !pending ? "安装中…" : "安装"}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {notice ? <p className="skill-validation is-ok">{notice}</p> : null}
      {error ? (
        <p className="proposal-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="skill-form-actions">
        <button onClick={onCancel} type="button">
          取消
        </button>
      </div>
      {pending && !pending.upgrade ? (
        <ConfirmDialog
          body={`将从 ${pending.hit.spec} 下载技能包，经过静态扫描后安装到「${
            scope === "user" ? "用户级" : "当前工作区"
          }」。安装量 ${pending.hit.installs}，来源会记录在技能详情里。`}
          confirmLabel="确认安装"
          onClose={() => setPending(null)}
          onConfirm={() => void install(pending.hit, false)}
          title="从生态安装这个技能？"
        />
      ) : null}
      {pending?.upgrade ? (
        <ConfirmDialog
          body={`同名技能「${pending.hit.skill || pending.hit.spec}」已存在且内容不同。继续会归档旧版本并激活新版本，可手工回滚。`}
          confirmLabel="确认升级"
          onClose={() => setPending(null)}
          onConfirm={() => void install(pending.hit, true)}
          title="升级已存在的技能？"
        />
      ) : null}
    </section>
  );
}
