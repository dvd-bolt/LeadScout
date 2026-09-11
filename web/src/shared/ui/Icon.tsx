import type { SVGProps } from "react";

const paths = {
  arrow: "M5 19 19 5M5 5h14v14",
  home: "m3 11 9-8 9 8v10h-6v-7H9v7H3Z",
  inbox: "M4 3h16v18H4ZM8 7h8M8 11h8M4 16h5l1 2h4l1-2h5",
  resume: "M5 3h9l5 5v13H5ZM14 3v6h5M9 13h6M9 17h6",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM10 2h4l1 3 3 1 3 3-1 3 1 3-3 3-3 1-1 3h-4l-1-3-3-1-3-3 1-3-1-3 3-3 3-1Z",
  user: "M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM4 21v-2a8 6 0 0 1 16 0v2Z",
  users: "M9 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM2 21v-3a7 5 0 0 1 14 0v3ZM17 4a3 3 0 0 1 0 6M18 14a5 4 0 0 1 4 4v3",
  chevron: "m9 5 7 7-7 7",
  clock: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20ZM12 6v6l4 2",
  play: "m6 3 15 9-15 9Z",
  stop: "M5 5h14v14H5Z",
  sliders: "M3 5h7m4 0h7M3 12h12m4 0h2M3 19h3m4 0h11M10 2v6M15 9v6M6 16v6",
  send: "m3 10 19-8-7 20-4-8-8-4ZM11 14 22 2",
  chat: "M4 3h16v14H10l-6 4ZM8 7h8M8 11h5",
  check: "m5 12 4 4L19 6",
  alert: "M12 3 2 21h20ZM12 9v5M12 18h.01",
  info: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20ZM12 11v6M12 7h.01",
} as const;
export type IconName = keyof typeof paths;
export function Icon({ name, size = 24, ...props }: SVGProps<SVGSVGElement> & { name: IconName; size?: number }) {
  const filled = name === "play" || name === "stop" || name === "user" || name === "home";
  return <svg width={size} height={size} viewBox="0 0 24 24" fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth={name === "arrow" ? 4 : 1.9} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}><path d={paths[name]} /></svg>;
}
