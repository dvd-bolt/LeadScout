import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "../shared/http/client";
import type { ReactNode } from "react";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: (count, error) => !(error instanceof ApiError && [401, 403, 409].includes(error.status)) && count < 1, refetchOnWindowFocus: true } },
});

export function AppProviders({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
