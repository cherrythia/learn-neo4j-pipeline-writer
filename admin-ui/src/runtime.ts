import { notifications } from "@mantine/notifications";
import { QueryClient } from "@tanstack/react-query";
import { createApi, type Api, type ErrorSink } from "./api/client";
import { ME_KEY } from "./auth";

export interface Runtime {
  api: Api;
  queryClient: QueryClient;
}

/** Wire the API client's error mapping into the app: 401 -> Login, the rest -> notifications. */
export function createRuntime(options: { baseUrl?: string; fetch?: (r: Request) => Promise<Response>; notify?: ErrorSink["notify"] } = {}): Runtime {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  const api = createApi({
    baseUrl: options.baseUrl,
    fetch: options.fetch,
    sink: {
      onUnauthorized: () => queryClient.setQueryData(ME_KEY, null),
      notify: options.notify ?? ((n) => notifications.show({ ...n, autoClose: 8000 })),
    },
  });
  return { api, queryClient };
}
