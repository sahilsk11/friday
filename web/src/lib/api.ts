import { fridayBaseUrl } from './env';

// The *only* file allowed to touch global fetch — enforced by ESLint's
// no-restricted-syntax rule. Every other module goes through the
// helpers below.
//
// Why a wrapper at all: keeps JSON parsing, error surfacing, and
// auth-header injection in one place. When Step 6 lands a bearer
// token, this is the one file that changes.

export interface ApiError extends Error {
  status: number;
  body: unknown;
}

class ApiErrorImpl extends Error implements ApiError {
  status: number;
  body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return (
    error instanceof Error &&
    error.name === 'ApiError' &&
    'status' in error &&
    typeof error.status === 'number'
  );
}

export function apiUrl(path: string): string {
  if (!path.startsWith('/')) {
    throw new Error(`api path must start with "/": ${path}`);
  }
  return `${fridayBaseUrl}${path}`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const url = apiUrl(path);
  const init: RequestInit = {
    method,
    headers: {
      Accept: 'application/json',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  };

  const res = await fetch(url, init);

  // 202/204 may be empty — return undefined typed as T; the caller's
  // type contract owns whether that's safe.
  if (res.status === 204) {
    return undefined as T;
  }

  let parsed: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      // Non-JSON body — keep the raw text on the error for context.
      parsed = text;
    }
  }

  if (!res.ok) {
    const message =
      parsed &&
      typeof parsed === 'object' &&
      'detail' in parsed &&
      typeof parsed.detail === 'string'
        ? parsed.detail
        : `${res.status.toString()} ${res.statusText}`;
    throw new ApiErrorImpl(message, res.status, parsed);
  }

  return parsed as T;
}

export const apiClient = {
  get<T>(path: string): Promise<T> {
    return request<T>('GET', path);
  },
  post<T>(path: string, body?: unknown): Promise<T> {
    return request<T>('POST', path, body);
  },
  patch<T>(path: string, body?: unknown): Promise<T> {
    return request<T>('PATCH', path, body);
  },
  delete<T>(path: string): Promise<T> {
    return request<T>('DELETE', path);
  },
};
