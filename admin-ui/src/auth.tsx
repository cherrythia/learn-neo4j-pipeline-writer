import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "./api/context";
import type { Me } from "./api/types";

export const ME_KEY = ["auth", "me"] as const;

/** The logged-in user, or null (401). */
export function useMe() {
  const api = useApi();
  return useQuery({
    queryKey: ME_KEY,
    queryFn: async (): Promise<Me | null> => {
      const { data, response } = await api.GET("/api/auth/me");
      if (response.status === 401) return null;
      if (!response.ok || !data) throw new Error(`could not load the current user (${response.status})`);
      return data;
    },
    retry: false,
    staleTime: Infinity,
  });
}

export function useLogout() {
  const api = useApi();
  const qc = useQueryClient();
  return async () => {
    await api.POST("/api/auth/logout");
    // Keep the "me" query (its observer drives the Login screen); drop everything else.
    qc.setQueryData(ME_KEY, null);
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== ME_KEY[0] });
  };
}
