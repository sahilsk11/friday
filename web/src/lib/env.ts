function resolveBaseUrl(): string {
  const override = (import.meta.env.VITE_FRIDAY_BASE_URL as string | undefined)?.replace(/\/$/, '');
  if (override) {
    return override;
  }

  if (typeof window !== 'undefined') {
    return `${window.location.protocol}//${window.location.host}`;
  }

  return 'http://localhost:8000';
}

export const fridayBaseUrl = resolveBaseUrl();
