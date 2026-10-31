import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";
import type { UserRole, UserStatus } from "../api/user-contracts";

export const USERS_QUERY_KEY = ["users"] as const;
export const MY_AI_SERVICES_QUERY_KEY = ["my-ai-services"] as const;

export interface UserFilters {
  includeDeleted?: boolean;
  role?: UserRole;
  search?: string;
  status?: UserStatus;
}

export function userQueryKey(id: string) {
  return ["users", id] as const;
}

export function useUsers(apiClient: ApiClient, filters: UserFilters = {}) {
  return useQuery({
    queryFn: () => apiClient.getUsers(filters),
    queryKey: [...USERS_QUERY_KEY, filters],
  });
}

export function useUser(apiClient: ApiClient, id: string, enabled = true) {
  return useQuery({
    enabled: enabled && id.length > 0,
    queryFn: () => apiClient.getUser(id),
    queryKey: userQueryKey(id),
  });
}

export function useMyAiServices(apiClient: ApiClient) {
  return useQuery({
    queryFn: () => apiClient.getMyAiServices(),
    queryKey: MY_AI_SERVICES_QUERY_KEY,
  });
}
