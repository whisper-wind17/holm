import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { BrowserRouter } from "react-router";

import { ApiError, browserApiClient, type ApiClient } from "../api/client";
import { SessionProvider } from "../auth/session";
import { AppErrorBoundary } from "./error-boundary";

export function createAppQueryClient() {
  return new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: {
        refetchOnWindowFocus: false,
        retry: (failureCount, error) =>
          error instanceof ApiError && error.status >= 500 && failureCount < 2,
        staleTime: 30_000,
      },
    },
  });
}

export interface AppProvidersProps {
  apiClient?: ApiClient;
  children: ReactNode;
  queryClient?: QueryClient;
}

export function AppProviders({
  apiClient = browserApiClient,
  children,
  queryClient,
}: AppProvidersProps) {
  const [ownedQueryClient] = useState(createAppQueryClient);
  const activeQueryClient = queryClient ?? ownedQueryClient;

  return (
    <QueryClientProvider client={activeQueryClient}>
      <AppErrorBoundary>
        <BrowserRouter>
          <SessionProvider apiClient={apiClient}>{children}</SessionProvider>
        </BrowserRouter>
      </AppErrorBoundary>
    </QueryClientProvider>
  );
}
