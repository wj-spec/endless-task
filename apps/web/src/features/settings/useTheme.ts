import { useCallback, useEffect, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";

const THEME_KEY = "endless-task-theme";

const readPreference = (): ThemePreference => {
  const stored = localStorage.getItem(THEME_KEY);
  return stored === "light" || stored === "dark" ? stored : "system";
};

const resolve = (preference: ThemePreference): "light" | "dark" => {
  if (preference === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }
  return preference;
};

export function applyTheme(preference: ThemePreference) {
  document.documentElement.dataset.theme = resolve(preference);
}

export function applyStoredTheme() {
  applyTheme(readPreference());
}

export function useTheme() {
  const [preference, setPreferenceState] = useState<ThemePreference>(readPreference);

  useEffect(() => {
    applyTheme(preference);
    if (preference !== "system") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [preference]);

  const setPreference = useCallback((next: ThemePreference) => {
    localStorage.setItem(THEME_KEY, next);
    setPreferenceState(next);
  }, []);

  return { preference, setPreference };
}
