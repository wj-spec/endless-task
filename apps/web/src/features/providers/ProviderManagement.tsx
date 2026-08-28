import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import type { ProviderProfile } from "../chat/apiTypes";
import { EmptyState } from "../ui/EmptyState";

type ProviderManagementProps = {
  onChanged: () => void | Promise<void>;
};

type ProviderForm = {
  name: string;
  defaultModel: string;
  baseUrl: string;
  apiKeyRef: string;
  timeoutSeconds: string;
};

const emptyForm: ProviderForm = {
  name: "",
  defaultModel: "",
  baseUrl: "",
  apiKeyRef: "",
  timeoutSeconds: "60",
};

const formFromProfile = (profile: ProviderProfile): ProviderForm => ({
  name: profile.name,
  defaultModel: profile.defaultModel,
  baseUrl: profile.baseUrl,
  apiKeyRef: "",
  timeoutSeconds: String(profile.timeoutSeconds),
});

const formPayload = (form: ProviderForm, includeApiKey: boolean) => ({
  name: form.name.trim(),
  defaultModel: form.defaultModel.trim(),
  baseUrl: form.baseUrl.trim(),
  ...(includeApiKey ? { apiKeyRef: form.apiKeyRef.trim() } : {}),
  timeoutSeconds: Number(form.timeoutSeconds || "60"),
});

const actionErrorMessage = (error: unknown, fallback: string) =>
  error instanceof Error && error.message ? error.message : fallback;

export function ProviderManagement({ onChanged }: ProviderManagementProps) {
  const [profiles, setProfiles] = useState<ProviderProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<ProviderForm>(emptyForm);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<ProviderForm>(emptyForm);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setProfiles(await chatApi.listProviders());
      setLoadError(null);
    } catch {
      setLoadError("无法加载模型配置，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const updateForm = (patch: Partial<ProviderForm>) => {
    setForm((current) => ({ ...current, ...patch }));
  };

  const updateEditForm = (patch: Partial<ProviderForm>) => {
    setEditForm((current) => ({ ...current, ...patch }));
  };

  const createProfile = async () => {
    setActionError(null);
    try {
      await chatApi.createProvider(formPayload(form, true));
      setForm(emptyForm);
      setAdding(false);
      await load();
      await onChanged();
    } catch (error) {
      setActionError(actionErrorMessage(error, "添加模型配置失败，请重试。"));
    }
  };

  const saveProfile = async (profile: ProviderProfile) => {
    setBusyId(profile.id);
    setActionError(null);
    try {
      await chatApi.patchProvider(
        profile.id,
        formPayload(editForm, editForm.apiKeyRef.trim().length > 0),
      );
      setEditingId(null);
      await load();
      await onChanged();
    } catch (error) {
      setActionError(actionErrorMessage(error, "保存模型配置失败，请重试。"));
    } finally {
      setBusyId(null);
    }
  };

  const toggleEnabled = async (profile: ProviderProfile) => {
    setBusyId(profile.id);
    setActionError(null);
    try {
      await chatApi.patchProvider(profile.id, { enabled: !profile.enabled });
      await load();
      await onChanged();
    } catch (error) {
      setActionError(actionErrorMessage(error, "更新模型配置失败，请重试。"));
    } finally {
      setBusyId(null);
    }
  };

  const setDefault = async (profile: ProviderProfile) => {
    setBusyId(profile.id);
    setActionError(null);
    try {
      await chatApi.setDefaultProvider(profile.id);
      await load();
      await onChanged();
    } catch (error) {
      setActionError(actionErrorMessage(error, "设置默认模型失败，请重试。"));
    } finally {
      setBusyId(null);
    }
  };

  const removeProfile = async (profile: ProviderProfile) => {
    if (!window.confirm(`删除模型配置「${profile.name}」？`)) return;
    setBusyId(profile.id);
    setActionError(null);
    try {
      await chatApi.deleteProvider(profile.id);
      await load();
      await onChanged();
    } catch (error) {
      setActionError(actionErrorMessage(error, "删除模型配置失败，请重试。"));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="panel-content">
      <div className="knowledge-toolbar">
        <p className="provider-summary">
          支持 OpenAI-compatible 服务；云端 API Key 仅填写环境变量引用，本地端点可留空。
        </p>
        <button
          onClick={() => {
            setAdding((value) => !value);
            setForm(emptyForm);
          }}
          type="button"
        >
          {adding ? "取消" : "添加模型"}
        </button>
      </div>
      {actionError ? (
        <div className="proposal-error" role="alert">
          {actionError}
        </div>
      ) : null}
      {adding ? (
        <div className="knowledge-form provider-form">
          <input
            aria-label="模型名称"
            onChange={(event) => updateForm({ name: event.target.value })}
            placeholder="名称（如 DeepSeek）"
            value={form.name}
          />
          <input
            aria-label="默认模型"
            onChange={(event) => updateForm({ defaultModel: event.target.value })}
            placeholder="默认模型（如 deepseek-chat）"
            value={form.defaultModel}
          />
          <input
            aria-label="Base URL"
            onChange={(event) => updateForm({ baseUrl: event.target.value })}
            placeholder="Base URL（如 https://api.deepseek.com/v1）"
            value={form.baseUrl}
          />
          <input
            aria-label="API Key 引用"
            onChange={(event) => updateForm({ apiKeyRef: event.target.value })}
            placeholder={'API Key（仅支持 ${ENV_VAR}）'}
            value={form.apiKeyRef}
          />
          <input
            aria-label="超时时间（秒）"
            inputMode="numeric"
            onChange={(event) => updateForm({ timeoutSeconds: event.target.value })}
            placeholder="超时秒数"
            value={form.timeoutSeconds}
          />
          <button
            disabled={!form.name.trim() || !form.defaultModel.trim()}
            onClick={() => void createProfile()}
            type="button"
          >
            保存
          </button>
        </div>
      ) : null}
      {loading ? <EmptyState title="正在加载模型配置" /> : null}
      {loadError ? <EmptyState title={loadError} /> : null}
      <div className="provider-list">
        {profiles.map((profile) => (
          <article className={`provider-card${profile.enabled ? "" : " is-disabled"}`} key={profile.id}>
            <header>
              <div>
                <strong>{profile.name}</strong>
                <span className="provider-model">{profile.defaultModel}</span>
              </div>
              <div className="provider-badges">
                {profile.isDefault ? <span className="knowledge-badge is-global">默认</span> : null}
                {profile.isBuiltin ? <span className="knowledge-badge">环境配置</span> : null}
                <span className={`knowledge-badge${profile.configured ? "" : " is-warning"}`}>
                  {profile.configured ? "已配置" : "未配置"}
                </span>
                {!profile.enabled ? <span className="knowledge-badge">停用</span> : null}
              </div>
            </header>
            <dl>
              <div>
                <dt>Base URL</dt>
                <dd>{profile.baseUrl || "官方默认地址"}</dd>
              </div>
              <div>
                <dt>API Key</dt>
                <dd>{profile.configured ? "已设置" : "未设置"}</dd>
              </div>
              <div>
                <dt>超时</dt>
                <dd>{profile.timeoutSeconds} 秒</dd>
              </div>
            </dl>
            {editingId === profile.id ? (
              <div className="knowledge-form provider-form">
                <input
                  aria-label="编辑模型名称"
                  onChange={(event) => updateEditForm({ name: event.target.value })}
                  value={editForm.name}
                />
                <input
                  aria-label="编辑默认模型"
                  onChange={(event) => updateEditForm({ defaultModel: event.target.value })}
                  value={editForm.defaultModel}
                />
                <input
                  aria-label="编辑 Base URL"
                  onChange={(event) => updateEditForm({ baseUrl: event.target.value })}
                  value={editForm.baseUrl}
                />
                <input
                  aria-label="编辑 API Key 引用"
                  onChange={(event) => updateEditForm({ apiKeyRef: event.target.value })}
                  placeholder={profile.isBuiltin ? "由环境配置维护" : "留空表示不修改 API Key"}
                  value={editForm.apiKeyRef}
                />
                <input
                  aria-label="编辑超时时间（秒）"
                  inputMode="numeric"
                  onChange={(event) => updateEditForm({ timeoutSeconds: event.target.value })}
                  value={editForm.timeoutSeconds}
                />
                <div className="provider-form-actions">
                  <button disabled={busyId === profile.id} onClick={() => void saveProfile(profile)} type="button">
                    保存
                  </button>
                  <button onClick={() => setEditingId(null)} type="button">
                    取消
                  </button>
                </div>
              </div>
            ) : (
              <footer>
                <button
                  disabled={profile.isDefault || busyId === profile.id}
                  onClick={() => void setDefault(profile)}
                  type="button"
                >
                  设为默认
                </button>
                <button
                  disabled={profile.isBuiltin || busyId === profile.id}
                  onClick={() => {
                    setEditingId(profile.id);
                    setEditForm(formFromProfile(profile));
                  }}
                  type="button"
                >
                  编辑
                </button>
                <button
                  disabled={profile.isDefault || busyId === profile.id}
                  onClick={() => void toggleEnabled(profile)}
                  type="button"
                >
                  {profile.enabled ? "停用" : "启用"}
                </button>
                {profile.isBuiltin ? null : (
                  <button
                    className="danger-button"
                    disabled={busyId === profile.id}
                    onClick={() => void removeProfile(profile)}
                    type="button"
                  >
                    删除
                  </button>
                )}
              </footer>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}
