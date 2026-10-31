import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { ApiError, type ApiClient } from "../api/client";
import type { MeResponse } from "../api/contracts";

export const SESSION_QUERY_KEY = ["session", "me"] as const;

interface SessionContextValue {
  clear: () => void;
  me: MeResponse | undefined;
  refresh: () => Promise<MeResponse>;
  status: "anonymous" | "authenticated" | "loading";
}

const SessionContext = createContext<SessionContextValue | undefined>(
  undefined,
);

export interface SessionProviderProps {
  apiClient: ApiClient;
  children: ReactNode;
}

export function SessionProvider({ apiClient, children }: SessionProviderProps) {
  const queryClient = useQueryClient();
  const [forcedAnonymous, setForcedAnonymous] = useState(false);
  const sessionQuery = useQuery({
    enabled: !forcedAnonymous,
    queryFn: () => apiClient.getMe(),
    queryKey: SESSION_QUERY_KEY,
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  });

  const clear = useMemo(
    () => () => {
      setForcedAnonymous(true);
      queryClient.removeQueries({ queryKey: SESSION_QUERY_KEY });
    },
    [queryClient],
  );

  useEffect(() => {
    apiClient.setAuthenticationFailureHandler(clear);
    return () => {
      apiClient.setAuthenticationFailureHandler(undefined);
    };
  }, [apiClient, clear]);

  async function refresh() {
    setForcedAnonymous(false);
    return queryClient.query({
      queryFn: () => apiClient.getMe(),
      queryKey: SESSION_QUERY_KEY,
      staleTime: 0,
    });
  }

  if (
    sessionQuery.error !== null &&
    !(
      sessionQuery.error instanceof ApiError &&
      sessionQuery.error.status === 401
    )
  ) {
    throw sessionQuery.error;
  }

  const me = forcedAnonymous ? undefined : sessionQuery.data;
  const status = forcedAnonymous
    ? "anonymous"
    : me === undefined
      ? sessionQuery.isPending
        ? "loading"
        : "anonymous"
      : "authenticated";

  return (
    <SessionContext.Provider value={{ clear, me, refresh, status }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession() {
  const context = useContext(SessionContext);
  if (context === undefined) {
    throw new Error("useSession must be used inside SessionProvider");
  }
  return context;
}
