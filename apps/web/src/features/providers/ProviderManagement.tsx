import { useCallback, useEffect, useState } from "react";
import { FormErrorSummary } from "../ui/FormErrorSummary";
import { ApiClientError, chatApi } from "../chat/api";
import type { ProviderModel, ProviderProfile } from "../chat/apiTypes";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { EmptyState } from "../ui/EmptyState";

type ProviderManagementProps = {
  onChanged: () => void | Promise<void>;
};

type ProviderForm = {
  name: string;
  baseUrl: string;
  apiKey: string;
  timeoutSeconds: string;
  initialModel: string;
};

const emptyForm: ProviderForm = {
  name: "",
  baseUrl: "",
  apiKey: "",
  timeoutSeconds: "60",
  initialModel: "",
};

const formFromProfile = (profile: ProviderProfile): ProviderForm => ({
  name: profile.name,
  baseUrl: profile.baseUrl,
  apiKey: "",
  timeoutSeconds: String(profile.timeoutSeconds),
  initialModel: profile.defaultModel,
});

const profilePayload = (form: ProviderForm, includeApiKey: boolean) => ({
  name: form.name.trim(),
  baseUrl: form.baseUrl.trim(),
  defaultModel: form.initialModel.trim(),
  ...(includeApiKey ? { apiKey: form.apiKey.trim() } : {}),
  timeoutSeconds: Number(form.timeoutSeconds || "60"),
});

const productErrorCodes = new Set([
  "authentication_failed",
  "builtin_provider_credentials",
  "content_filtered",
  "context_too_large",
  "incomplete_stream",
  "invalid_provider_credentials",
  "invalid_request",
  "model_discovery_unavailable",
  "model_not_found",
  "network_error",
  "permission_denied",
  "provider_error",
  "provider_not_configured",
  "provider_unavailable",
  "rate_limited",
  "request_timeout",
]);

const actionErrorMessage = (error: unknown, fallback: string) =>
  error instanceof ApiClientError && productErrorCodes.has(error.code)
    ? error.message
    : fallback;

const connectionLabel = (profile: ProviderProfile) => {
  if (!profile.configured) return "未配置";
  if (profile.connectionState === "failed") return "连接失败";
  if (profile.connectionState === "ready") return "连接正常";
  return "未验证";
};

export function ProviderManagement({ onChanged }: ProviderManagementProps) {
  const [profiles, setProfiles] = useState<ProviderProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<ProviderForm>(emptyForm);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<ProviderForm>(emptyForm);
  const [addingModelFor, setAddingModelFor] = useState<string | null>(null);
  const [modelIdDraft, setModelIdDraft] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deletingProfile, setDeletingProfile] =
    useState<ProviderProfile | null>(null);
  const [deletingCredentialsFor, setDeletingCredentialsFor] =
    useState<ProviderProfile | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setProfiles(await chatApi.listProviders());
      setLoadError(null);
    } catch {
      setLoadError("无法加载模型服务，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const reloadAll = async () => {
    await load();
    await onChanged();
  };

  const updateForm = (patch: Partial<ProviderForm>) => {
    setForm((current) => ({ ...current, ...patch }));
  };

  const updateEditForm = (patch: Partial<ProviderForm>) => {
    setEditForm((current) => ({ ...current, ...patch }));
  };

  const createProfile = async () => {
    setBusyId("create");
    setActionError(null);
    try {
      await chatApi.createProvider(profilePayload(form, Boolean(form.apiKey.trim())));
      setForm(emptyForm);
      setAdding(false);
      await reloadAll();
    } catch (error) {
      setActionError(actionErrorMessage(error, "添加模型服务失败，请重试。"));
    } finally {
      setBusyId(null);
    }
  };

  const saveProfile = async (profile: ProviderProfile) => {
    setBusyId(profile.id);
    setActionError(null);
    try {
      await chatApi.patchProvider(
        profile.id,
        profilePayload(editForm, Boolean(editForm.apiKey.trim())),
      );
      setEditingId(null);
      await reloadAll();
    } catch (error) {
      setActionError(actionErrorMessage(error, "保存模型服务失败，请重试。"));
    } finally {
      setBusyId(null);
    }
  };

  const runProfileAction = async (
    profile: ProviderProfile,
    action: () => Promise<unknown>,
    fallback: string,
  ) => {
    setBusyId(profile.id);
    setActionError(null);
    try {
      await action();
      await reloadAll();
    } catch (error) {
      setActionError(actionErrorMessage(error, fallback));
    } finally {
      setBusyId(null);
    }
  };

  const addModel = async (profile: ProviderProfile) => {
    const modelId = modelIdDraft.trim();
    if (!modelId) return;
    await runProfileAction(
      profile,
      () => chatApi.addProviderModel(profile.id, { modelId }),
      "添加模型失败，请重试。",
    );
    setAddingModelFor(null);
    setModelIdDraft("");
  };

  const updateModel = async (
    profile: ProviderProfile,
    model: ProviderModel,
    enabled: boolean,
  ) => {
    await runProfileAction(
      profile,
      () => chatApi.patchProviderModel(profile.id, model.modelId, { enabled }),
      "更新模型失败，请重试。",
    );
  };

  return (
    <div className="panel-content">
      <div className="knowledge-toolbar">
        <p className="provider-summary">
          在这里接入模型服务并维护可用模型。API Key 只保存在本地后端，不会回传到浏览器。
        </p>
        <button
          className="knowledge-add-button"
          onClick={() => {
            setAdding((value) => !value);
            setForm(emptyForm);
          }}
          type="button"
        >
          {adding ? "取消" : "添加模型服务"}
        </button>
      </div>

      <FormErrorSummary
        error={actionError}
        heading="模型服务操作没有完成"
        className="proposal-error form-error-summary"
      />

      {adding ? (
        <div className="knowledge-form provider-form">
          <label>
            <span>服务名称</span>
            <input
              aria-label="服务名称"
              onChange={(event) => updateForm({ name: event.target.value })}
              placeholder="例如 DeepSeek"
              value={form.name}
            />
          </label>
          <label>
            <span>Base URL</span>
            <input
              aria-label="Base URL"
              onChange={(event) => updateForm({ baseUrl: event.target.value })}
              placeholder="例如 https://api.deepseek.com/v1"
              value={form.baseUrl}
            />
          </label>
          <label>
            <span>API Key</span>
            <input
              aria-label="API Key"
              autoComplete="new-password"
              onChange={(event) => updateForm({ apiKey: event.target.value })}
              placeholder="输入后仅发送到本地后端"
              type="password"
              value={form.apiKey}
            />
          </label>
          <label>
            <span>初始模型（可选）</span>
            <input
              aria-label="初始模型"
              onChange={(event) => updateForm({ initialModel: event.target.value })}
              placeholder="服务不支持自动发现时填写模型 ID"
              value={form.initialModel}
            />
          </label>
          <label>
            <span>超时时间（秒）</span>
            <input
              aria-label="超时时间（秒）"
              inputMode="numeric"
              onChange={(event) => updateForm({ timeoutSeconds: event.target.value })}
              value={form.timeoutSeconds}
            />
          </label>
          <div className="provider-form-actions">
            <button
              aria-busy={busyId === "create"}
              disabled={!form.name.trim() || busyId === "create"}
              onClick={() => void createProfile()}
              type="button"
            >
              保存服务
            </button>
          </div>
        </div>
      ) : null}

      {loading ? <EmptyState title="正在加载模型服务" /> : null}
      {loadError ? <EmptyState title={loadError} /> : null}

      <div className="provider-list">
        {profiles.map((profile) => (
          <article
            className={`provider-card${profile.enabled ? "" : " is-disabled"}`}
            key={profile.id}
          >
            <header>
              <div>
                <strong>{profile.name}</strong>
                <span className="provider-model">
                  {profile.defaultModel
                    ? `默认模型 · ${profile.defaultModel}`
                    : "尚未选择默认模型"}
                </span>
              </div>
              <div className="provider-badges">
                {profile.isDefault ? (
                  <span className="knowledge-badge is-global">默认服务</span>
                ) : null}
                {profile.isBuiltin ? (
                  <span className="knowledge-badge">环境配置</span>
                ) : null}
                <span
                  className={`knowledge-badge${
                    profile.connectionState === "failed" || !profile.configured
                      ? " is-warning"
                      : ""
                  }`}
                >
                  {connectionLabel(profile)}
                </span>
                {!profile.enabled ? (
                  <span className="knowledge-badge">已停用</span>
                ) : null}
              </div>
            </header>

            <dl>
              <div>
                <dt>Base URL</dt>
                <dd>{profile.baseUrl || "服务商默认地址"}</dd>
              </div>
              <div>
                <dt>API Key</dt>
                <dd>{profile.apiKeyConfigured ? "已保存在本地后端" : "未设置"}</dd>
              </div>
              <div>
                <dt>超时</dt>
                <dd>{profile.timeoutSeconds} 秒</dd>
              </div>
            </dl>

            {profile.lastError ? (
              <p className="provider-connection-error" role="status">
                无法验证模型服务，请检查服务地址和 API Key 后重试。
              </p>
            ) : null}

            {editingId === profile.id ? (
              <div className="knowledge-form provider-form provider-edit-form">
                <label>
                  <span>服务名称</span>
                  <input
                    aria-label="编辑服务名称"
                    onChange={(event) =>
                      updateEditForm({ name: event.target.value })
                    }
                    value={editForm.name}
                  />
                </label>
                <label>
                  <span>Base URL</span>
                  <input
                    aria-label="编辑 Base URL"
                    onChange={(event) =>
                      updateEditForm({ baseUrl: event.target.value })
                    }
                    value={editForm.baseUrl}
                  />
                </label>
                <label>
                  <span>替换 API Key</span>
                  <input
                    aria-label="替换 API Key"
                    autoComplete="new-password"
                    onChange={(event) =>
                      updateEditForm({ apiKey: event.target.value })
                    }
                    placeholder="留空表示不修改"
                    type="password"
                    value={editForm.apiKey}
                  />
                </label>
                <label>
                  <span>超时时间（秒）</span>
                  <input
                    aria-label="编辑超时时间（秒）"
                    inputMode="numeric"
                    onChange={(event) =>
                      updateEditForm({ timeoutSeconds: event.target.value })
                    }
                    value={editForm.timeoutSeconds}
                  />
                </label>
                <div className="provider-form-actions">
                  <button
                    aria-busy={busyId === profile.id}
                    disabled={busyId === profile.id || !editForm.name.trim()}
                    onClick={() => void saveProfile(profile)}
                    type="button"
                  >
                    保存
                  </button>
                  <button onClick={() => setEditingId(null)} type="button">
                    取消
                  </button>
                </div>
              </div>
            ) : null}

            <section className="provider-models" aria-label={`${profile.name} 可用模型`}>
              <div className="provider-models-heading">
                <div>
                  <strong>可用模型</strong>
                  <span>{profile.models.filter((model) => model.enabled).length} 个已启用</span>
                </div>
                <button
                  disabled={busyId === profile.id}
                  onClick={() => {
                    setAddingModelFor(
                      addingModelFor === profile.id ? null : profile.id,
                    );
                    setModelIdDraft("");
                  }}
                  type="button"
                >
                  手工添加
                </button>
              </div>

              {addingModelFor === profile.id ? (
                <div className="provider-model-add">
                  <input
                    aria-label={`${profile.name} 模型 ID`}
                    onChange={(event) => setModelIdDraft(event.target.value)}
                    placeholder="模型 ID，例如 deepseek-chat"
                    value={modelIdDraft}
                  />
                  <button
                    disabled={!modelIdDraft.trim() || busyId === profile.id}
                    onClick={() => void addModel(profile)}
                    type="button"
                  >
                    添加
                  </button>
                </div>
              ) : null}

              {profile.models.length ? (
                <ul className="provider-model-list">
                  {profile.models.map((model) => (
                    <li className={model.enabled ? "" : "is-disabled"} key={model.modelId}>
                      <div>
                        <strong>{model.displayName}</strong>
                        {model.displayName !== model.modelId ? (
                          <span>{model.modelId}</span>
                        ) : null}
                      </div>
                      <div className="provider-model-actions">
                        <span className="knowledge-badge">
                          {model.source === "discovered" ? "自动发现" : "手工添加"}
                        </span>
                        {model.isDefault ? (
                          <span className="knowledge-badge is-global">默认</span>
                        ) : (
                          <button
                            disabled={!model.enabled || busyId === profile.id}
                            onClick={() =>
                              void runProfileAction(
                                profile,
                                () =>
                                  chatApi.setProviderDefaultModel(
                                    profile.id,
                                    model.modelId,
                                  ),
                                "设置默认模型失败，请重试。",
                              )
                            }
                            type="button"
                          >
                            设为默认
                          </button>
                        )}
                        {!model.isDefault ? (
                          <button
                            disabled={busyId === profile.id}
                            onClick={() =>
                              void updateModel(profile, model, !model.enabled)
                            }
                            type="button"
                          >
                            {model.enabled ? "停用" : "启用"}
                          </button>
                        ) : null}
                        {model.source === "manual" && !model.isDefault ? (
                          <button
                            className="danger-button"
                            disabled={busyId === profile.id}
                            onClick={() =>
                              void runProfileAction(
                                profile,
                                () =>
                                  chatApi.deleteProviderModel(
                                    profile.id,
                                    model.modelId,
                                  ),
                                "移除模型失败，请重试。",
                              )
                            }
                            type="button"
                          >
                            移除
                          </button>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="provider-model-empty">
                  暂无模型。请刷新模型列表，或手工添加模型 ID。
                </p>
              )}
            </section>

            <footer>
              {!profile.isBuiltin ? (
                <button
                  disabled={busyId === profile.id || !profile.configured}
                  onClick={() =>
                    void runProfileAction(
                      profile,
                      () => chatApi.refreshProviderModels(profile.id),
                      "无法从该服务获取模型列表，可改为手工添加。",
                    )
                  }
                  type="button"
                >
                  测试并刷新模型
                </button>
              ) : null}
              <button
                disabled={
                  profile.isDefault ||
                  busyId === profile.id ||
                  !profile.enabled ||
                  !profile.defaultModel
                }
                onClick={() =>
                  void runProfileAction(
                    profile,
                    () => chatApi.setDefaultProvider(profile.id),
                    "设置默认模型服务失败，请重试。",
                  )
                }
                type="button"
              >
                设为默认服务
              </button>
              <button
                disabled={profile.isBuiltin || busyId === profile.id}
                onClick={() => {
                  setEditingId(profile.id);
                  setEditForm(formFromProfile(profile));
                }}
                type="button"
              >
                编辑服务
              </button>
              {!profile.isBuiltin && profile.apiKeyConfigured ? (
                <button
                  className="danger-button"
                  disabled={busyId === profile.id}
                  onClick={() => setDeletingCredentialsFor(profile)}
                  type="button"
                >
                  删除 API Key
                </button>
              ) : null}
              <button
                disabled={profile.isDefault || busyId === profile.id}
                onClick={() =>
                  void runProfileAction(
                    profile,
                    () =>
                      chatApi.patchProvider(profile.id, {
                        enabled: !profile.enabled,
                      }),
                    "更新模型服务失败，请重试。",
                  )
                }
                type="button"
              >
                {profile.enabled ? "停用服务" : "启用服务"}
              </button>
              {!profile.isBuiltin ? (
                <button
                  className="danger-button"
                  disabled={profile.isDefault || busyId === profile.id}
                  onClick={() => setDeletingProfile(profile)}
                  type="button"
                >
                  删除服务
                </button>
              ) : null}
            </footer>
          </article>
        ))}
      </div>

      {deletingCredentialsFor ? (
        <ConfirmDialog
          body={`将删除模型服务“${deletingCredentialsFor.name}”保存在本地后端的 API Key。删除后，依赖该密钥的请求将不可用。`}
          confirmLabel="删除 API Key"
          onClose={() => setDeletingCredentialsFor(null)}
          onConfirm={() =>
            void runProfileAction(
              deletingCredentialsFor,
              () =>
                chatApi.patchProvider(deletingCredentialsFor.id, {
                  apiKeyRef: "",
                }),
              "删除 API Key 失败，请重试。",
            )
          }
          title="删除已保存的 API Key？"
        />
      ) : null}

      {deletingProfile ? (
        <ConfirmDialog
          body={`将删除模型服务“${deletingProfile.name}”及其模型目录。使用该服务的对话会改用默认模型服务。`}
          confirmLabel="删除服务"
          onClose={() => setDeletingProfile(null)}
          onConfirm={() =>
            void runProfileAction(
              deletingProfile,
              () => chatApi.deleteProvider(deletingProfile.id),
              "删除模型服务失败，请重试。",
            )
          }
          title="删除模型服务"
        />
      ) : null}
    </div>
  );
}
