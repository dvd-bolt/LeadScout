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
  if (!app) return;
  app.ready();
  app.expand();
}
