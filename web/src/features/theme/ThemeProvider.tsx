import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { telegram } from "../../shared/telegram/sdk";

export type ThemeName = "light" | "dark" | "green" | "pink";

export const THEME_OPTIONS: ReadonlyArray<{ name: ThemeName; label: string; description: string }> = [
  { name: "light", label: "Светлая", description: "Чёрно-белая" },
  { name: "dark", label: "Тёмная", description: "Graphite + белый" },
  { name: "green", label: "Зелёная", description: "Sage + emerald" },
  { name: "pink", label: "Розовая", description: "Bubblegum pink" },
];

const STORAGE_KEY = "leadscout-theme";
const THEME_NAMES = new Set<ThemeName>(THEME_OPTIONS.map(({ name }) => name));
const TELEGRAM_COLORS: Record<ThemeName, { background: string; header: string }> = {
  light: { background: "#ffffff", header: "#ffffff" },
  dark: { background: "#0d0f10", header: "#0d0f10" },
  green: { background: "#f8f8f1", header: "#f8f8f1" },
  pink: { background: "#fff3f8", header: "#fff3f8" },
};

type ThemeContextValue = { theme: ThemeName; setTheme: (theme: ThemeName) => void };
const ThemeContext = createContext<ThemeContextValue | null>(null);

function initialTheme(): ThemeName {
  let theme: ThemeName = "light";
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored && THEME_NAMES.has(stored as ThemeName)) theme = stored as ThemeName;
  } catch {
    // Storage can be disabled in embedded browsers; the theme still works for this session.
  }
  document.documentElement.dataset.theme = theme;
  return theme;
}

function syncTelegramChrome(theme: ThemeName) {
  const app = telegram();
  if (!app) return;
  const colors = TELEGRAM_COLORS[theme];
  if (app.isVersionAtLeast?.("6.9")) app.setHeaderColor?.(colors.header);
  if (app.isVersionAtLeast?.("6.1")) app.setBackgroundColor?.(colors.background);
  if (app.isVersionAtLeast?.("7.10")) app.setBottomBarColor?.(colors.background);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<ThemeName>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    syncTelegramChrome(theme);
    try {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // Keep the selected theme active even when persistence is unavailable.
    }
  }, [theme]);

  return <ThemeContext.Provider value={{ theme, setTheme }}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
