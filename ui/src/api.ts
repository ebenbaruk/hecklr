import { useCallback, useEffect, useState } from "react";

/** An API failure, with the server's explanation (and validation problems, if any). */
export class ApiError extends Error {
  readonly status: number;
  readonly problems: string[];

  constructor(status: number, detail: string, problems: string[] = []) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.problems = problems;
  }
}

interface ErrorBody {
  detail?: unknown;
  problems?: unknown;
}

async function parse<T>(response: Response): Promise<T> {
  if (response.ok) {
    return (await response.json()) as T;
  }
  let body: ErrorBody = {};
  try {
    body = (await response.json()) as ErrorBody;
  } catch {
    // Not JSON (e.g. a proxy error page): the status text is all there is.
  }
  const detail = typeof body.detail === "string" ? body.detail : response.statusText;
  const problems = Array.isArray(body.problems) ? body.problems.map(String) : [];
  throw new ApiError(response.status, detail || `HTTP ${response.status}`, problems);
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return parse<T>(await fetch(path, { signal: signal ?? null }));
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  return parse<T>(
    await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

/** Builds `path?key=value&...`, skipping empty values and repeating array values. */
export function withQuery(
  path: string,
  params: Record<string, string | number | string[] | null | undefined>,
): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (Array.isArray(value)) {
      value.forEach((v) => {
        query.append(key, v);
      });
    } else if (value !== null && value !== undefined && value !== "") {
      query.append(key, String(value));
    }
  }
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}

export interface Loaded<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => void;
}

/** GETs `path` (none when `null`), refetching when it changes or on `reload()`. */
export function useApi<T>(path: string | null): Loaded<T> {
  const [state, setState] = useState<{ path: string | null; data?: T; error?: Error }>({
    path: null,
  });
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (path === null) {
      return;
    }
    const controller = new AbortController();
    getJson<T>(path, controller.signal).then(
      (data) => {
        setState({ path, data });
      },
      (error: unknown) => {
        if (!controller.signal.aborted) {
          setState({ path, error: error instanceof Error ? error : new Error(String(error)) });
        }
      },
    );
    return () => {
      controller.abort();
    };
  }, [path, generation]);

  const reload = useCallback(() => {
    setGeneration((g) => g + 1);
  }, []);
  const current = state.path === path;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : undefined,
    loading: path !== null && (!current || (state.data === undefined && !state.error)),
    reload,
  };
}
