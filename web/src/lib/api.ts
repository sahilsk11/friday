import { fridayBaseUrl } from '@/lib/env';

export interface ApiError extends Error {
  body: unknown;
  status: number;
}

class HttpError extends Error implements ApiError {
  body: unknown;
  status: number;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = 'ApiError';
    this.body = body;
    this.status = status;
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

export function getErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    return error.message;
  }

  if (error instanceof Error && error.message) {
    return error.message;
  }

  return 'Something went wrong while talking to the API.';
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(buildApiUrl(path), {
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: {
      Accept: 'application/json',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    method,
  });

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  const payload = parseResponseBody(text);

  if (!response.ok) {
    throw new HttpError(resolveErrorMessage(payload, response), response.status, payload);
  }

  return payload as T;
}

function buildApiUrl(path: string): string {
  if (!path.startsWith('/')) {
    throw new Error(`API paths must start with "/". Received "${path}".`);
  }

  return `${fridayBaseUrl}${path}`;
}

function parseResponseBody(body: string): unknown {
  if (!body) {
    return null;
  }

  try {
    return JSON.parse(body) as unknown;
  } catch {
    return body;
  }
}

function resolveErrorMessage(body: unknown, response: Response): string {
  if (typeof body === 'string' && body.trim()) {
    return body;
  }

  const record = typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : null;
  if (record) {
    const detail = record.detail;
    if (typeof detail === 'string' && detail.trim()) {
      return detail;
    }
  }

  return `${response.status} ${response.statusText}`;
}

export const apiClient = {
  delete<T>(path: string): Promise<T> {
    return request<T>('DELETE', path);
  },
  get<T>(path: string): Promise<T> {
    return request<T>('GET', path);
  },
  patch<T>(path: string, body?: unknown): Promise<T> {
    return request<T>('PATCH', path, body);
  },
  post<T>(path: string, body?: unknown): Promise<T> {
    return request<T>('POST', path, body);
  },
};
