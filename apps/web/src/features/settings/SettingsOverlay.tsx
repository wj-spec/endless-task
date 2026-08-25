import { useCallback, useEffect, useState } from "react";
import { ApiClientError, chatApi } from "../chat/api";
import type { PermissionMode } from "../chat/apiTypes";

type SettingsOverlayProps = {
  onClose: () => void;
  onModeChanged: (mode: PermissionMode) => void;
};

const PERMISSION_RANK: Record<PermissionMode, number> = {
  confirm_every_time: 0,
  trust_local_writes: 1,
  trust_all: 2,
};

const PERMISSION_OPTIONS: {
  mode: PermissionMode;
  label: string;
  description: string;
}[] = [
  {
    mode: "confirm_every_time",
    label: "逐项确认（默认）",
    description: "写操作与外部操作都需要逐项批准，最稳妥。",
  },
  {
    mode: "trust_local_writes",
    label: "信任本地写操作",
    description: "本地写操作自动执行；外部操作仍逐项确认。",
  },
  {
    mode: "trust_all",
    label: "信任全部操作",
    description: "本地写操作与外部操作都自动执行，请谨慎选择。",
  },
];

const DESKTOP_NOTIFICATIONS_KEY = "endless-task-desktop-notifications";

export function SettingsOverlay({ onClose, onModeChanged }: SettingsOverlayProps) {
  const [desktopNotifications, setDesktopNotifications] = useState(
    () => localStorage.getItem(DESKTOP_NOTIFICATIONS_KEY) === "1",
  );
  const [desktopUnsupported, setDesktopUnsupported] = useState(
    typeof Notification === "undefined",
  );

  const toggleDesktopNotifications = async () => {
    if (desktopNotifications) {
      localStorage.setItem(DESKTOP_NOTIFICATIONS_KEY, "0");
      setDesktopNotifications(false);
      return;
    }
    if (typeof Notification === "undefined") {
      setDesktopUnsupported(true);
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      setError("浏览器未授予通知权限，桌面通知保持关闭。");
      return;
    }
    localStorage.setItem(DESKTOP_NOTIFICATIONS_KEY, "1");
    setDesktopNotifications(true);
  };
  const [currentMode, setCurrentMode] = useState<PermissionMode | null>(null);
  const [pendingMode, setPendingMode] = useState<PermissionMode | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    chatApi
      .getPermissionSettings()
      .then((settings) => setCurrentMode(settings.mode))
      .catch(() => setError("无法加载权限设置，请重试。"));
  }, []);

  const applyMode = useCallback(
    async (mode: PermissionMode, acknowledge: boolean) => {
      setBusy(true);
      setError(null);
      try {
        const settings = await chatApi.setPermissionSettings(mode, acknowledge);
        setCurrentMode(settings.mode);
        setPendingMode(null);
        setAcknowledged(false);
        onModeChanged(settings.mode);
      } catch (applyError) {
        setError(
          applyError instanceof ApiClientError
            ? applyError.message
            : "权限设置保存失败，请重试。",
        );
      } finally {
        setBusy(false);
      }
    },
    [onModeChanged],
  );

  const handleSelect = (mode: PermissionMode) => {
    if (!currentMode || mode === currentMode || busy) return;
    if (PERMISSION_RANK[mode] < PERMISSION_RANK[currentMode]) {
      setPendingMode(null);
      void applyMode(mode, false);
      return;
    }
    setPendingMode(mode);
    setAcknowledged(false);
  };

  const pendingOption = PERMISSION_OPTIONS.find(
    (option) => option.mode === pendingMode,
  );

  return (
    <div aria-label="设置" className="overlay" role="dialog">
      <div className="overlay-panel">
        <header className="overlay-header">
          <h2>设置</h2>
          <button onClick={onClose} type="button">
            关闭
          </button>
        </header>
        <div className="overlay-body">
          <h3 className="settings-section-title">操作权限</h3>
          <p className="settings-section-hint">
            权限是产品级设置。提权后，被覆盖的操作不再逐项打断你；随时可以降回更严格的档位。
          </p>
          {error ? (
            <div className="proposal-error" role="alert">
              {error}
            </div>
          ) : null}
          {currentMode === null && !error ? (
            <div className="overlay-empty">正在加载…</div>
          ) : null}
          {currentMode !== null
            ? PERMISSION_OPTIONS.map((option) => {
                const selected = option.mode === currentMode;
                return (
                  <label
                    className={`permission-option${selected ? " is-selected" : ""}`}
                    key={option.mode}
                  >
                    <input
                      checked={selected}
                      disabled={busy}
                      name="permission-mode"
                      onChange={() => handleSelect(option.mode)}
                      type="radio"
                    />
                    <span className="permission-option-text">
                      <strong>{option.label}</strong>
                      <span>{option.description}</span>
                    </span>
                  </label>
                );
              })
            : null}
          <h3 className="settings-section-title">桌面通知</h3>
          <p className="settings-section-hint">
            标签页在后台时，任务执行结果以系统通知提醒；应用内通知不受此开关影响。
          </p>
          {desktopUnsupported ? (
            <div className="overlay-empty">当前浏览器不支持桌面通知。</div>
          ) : (
            <label className="permission-acknowledge">
              <input
                checked={desktopNotifications}
                onChange={() => void toggleDesktopNotifications()}
                type="checkbox"
              />
              后台时接收桌面通知
            </label>
          )}
          {pendingOption && currentMode !== null ? (
            <div className="permission-confirm">
              <p>
                即将提权为「{pendingOption.label}」。提权后，被覆盖的操作将自动执行，不再逐项询问。
              </p>
              <label className="permission-acknowledge">
                <input
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                  type="checkbox"
                />
                我已理解并接受提权后的自动执行范围
              </label>
              <div className="proposal-actions">
                <button
                  disabled={!acknowledged || busy}
                  onClick={() => void applyMode(pendingOption.mode, true)}
                  type="button"
                >
                  {busy ? "保存中…" : "确认提权"}
                </button>
                <button
                  disabled={busy}
                  onClick={() => {
                    setPendingMode(null);
                    setAcknowledged(false);
                  }}
                  type="button"
                >
                  取消
                </button>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
