import { useQuery } from '@tanstack/react-query';

import { getSession, listHarnesses, listModels, listSessions } from '@/features/sessions/api';
import { sessionQueryKeys } from '@/types/api';

export function useSessionsQuery() {
  return useQuery({
    queryKey: sessionQueryKeys.sessions,
    queryFn: listSessions,
    staleTime: 30_000,
  });
}

export function useSessionQuery(
  sessionId: string,
  options: { refetchInterval?: number | false } = {},
) {
  return useQuery({
    queryKey: sessionQueryKeys.session(sessionId),
    queryFn: () => getSession(sessionId),
    enabled: sessionId.length > 0,
    refetchInterval: options.refetchInterval,
    staleTime: 30_000,
  });
}

export function useHarnessesQuery() {
  return useQuery({
    queryKey: sessionQueryKeys.harnesses,
    queryFn: listHarnesses,
    staleTime: 5 * 60_000,
  });
}

export function useModelsQuery(harnessId: string) {
  return useQuery({
    queryKey: sessionQueryKeys.models(harnessId),
    queryFn: () => listModels(harnessId),
    enabled: harnessId.length > 0,
    staleTime: 5 * 60_000,
  });
}
