declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string;
        colorScheme: "light" | "dark";
        themeParams: Record<string, string>;
        ready(): void;
        expand(): void;
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
  if (!app) return;
  const applyTheme = () => {
    document.documentElement.dataset.theme = app.colorScheme;
    Object.entries(app.themeParams).forEach(([name, value]) => {
      document.documentElement.style.setProperty(`--tg-${name.replaceAll("_", "-")}`, value);
    });
  };
  applyTheme();
  app.ready();
  app.expand();
  app.onEvent("themeChanged", applyTheme);
}
