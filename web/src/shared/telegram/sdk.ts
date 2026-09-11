declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string;
        colorScheme: "light" | "dark";
        themeParams: Record<string, string>;
        ready(): void;
        expand(): void;
        isVersionAtLeast?(version: string): boolean;
        setHeaderColor?(color: string): void;
        setBackgroundColor?(color: string): void;
        setBottomBarColor?(color: string): void;
        BackButton: { show(): void; hide(): void; onClick(callback: () => void): void; offClick(callback: () => void): void };
        onEvent(event: string, callback: () => void): void;
        offEvent(event: string, callback: () => void): void;
      };
    };
  }
}

export function telegram() {
  return window.Telegram?.WebApp;
}

export function prepareTelegramTheme() {
  const app = telegram();
  document.documentElement.dataset.theme = "light";
  if (!app) return;
  const applyTheme = () => {
    document.documentElement.dataset.theme = "light";
    if (app.isVersionAtLeast?.("6.9")) app.setHeaderColor?.("#ffffff");
    if (app.isVersionAtLeast?.("6.1")) app.setBackgroundColor?.("#ffffff");
    if (app.isVersionAtLeast?.("7.10")) app.setBottomBarColor?.("#ffffff");
  };
  applyTheme();
  app.ready();
  app.expand();
  app.onEvent("themeChanged", applyTheme);
}
