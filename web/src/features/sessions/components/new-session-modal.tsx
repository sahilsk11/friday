import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useState } from 'react';
import { useNavigate } from 'react-router';

import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { createSession } from '@/features/sessions/api';
import { useHarnessesQuery, useModelsQuery } from '@/features/sessions/hooks';
import { getErrorMessage } from '@/lib/api';
import { sessionQueryKeys, type SessionRouteState } from '@/types/api';

const DEFAULT_DIRECTORY = '/Users/sahil/portfolio/friday-v3';

interface NewSessionModalProps {
  onOpenChange: (open: boolean) => void;
  open: boolean;
}

export function NewSessionModal({ onOpenChange, open }: NewSessionModalProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const harnessesQuery = useHarnessesQuery();
  const [title, setTitle] = useState('');
  const [directory, setDirectory] = useState(DEFAULT_DIRECTORY);
  const [harness, setHarness] = useState('');
  const [modelId, setModelId] = useState('');
  const effectiveHarness = harness || harnessesQuery.data?.[0]?.id || '';
  const modelsQuery = useModelsQuery(effectiveHarness);
  const effectiveModelId =
    modelId && modelsQuery.data?.models.some((model) => model.model_ref === modelId)
      ? modelId
      : modelsQuery.data?.default || modelsQuery.data?.models[0]?.model_ref || '';

  const createSessionMutation = useMutation({
    mutationFn: createSession,
    onSuccess: async (response) => {
      await queryClient.invalidateQueries({ queryKey: sessionQueryKeys.sessions });
      onOpenChange(false);
      const routeState: SessionRouteState = { sessionPayload: response };
      void navigate(`/sessions/${response.session_id}`, { state: routeState });
    },
  });

  const disableSubmit =
    createSessionMutation.isPending ||
    !directory.trim() ||
    !effectiveHarness ||
    !effectiveModelId ||
    harnessesQuery.isLoading ||
    modelsQuery.isLoading;

  return (
    <Dialog
      footer={
        <>
          <Button onClick={() => onOpenChange(false)} type="button" variant="ghost">
            cancel
          </Button>
          <Button
            disabled={disableSubmit}
            onClick={() => {
              if (disableSubmit) {
                return;
              }

              void createSessionMutation.mutateAsync({
                directory: directory.trim(),
                harness: effectiveHarness,
                model_id: effectiveModelId,
                title: title.trim() || undefined,
              });
            }}
            type="button"
          >
            {createSessionMutation.isPending ? 'creating...' : 'create'}
          </Button>
        </>
      }
      onOpenChange={onOpenChange}
      open={open}
      title="new session"
    >
      <div className="grid gap-4">
        <Field label="title">
          <Input
            onChange={(event) => setTitle(event.target.value)}
            placeholder="optional"
            value={title}
          />
        </Field>

        <Field label="directory">
          <Input
            onChange={(event) => setDirectory(event.target.value)}
            placeholder="/absolute/path"
            value={directory}
          />
        </Field>

        <Field label="harness">
          <Select
            disabled={harnessesQuery.isLoading || Boolean(harnessesQuery.error)}
            onChange={(event) => setHarness(event.target.value)}
            value={effectiveHarness}
          >
            {harnessesQuery.isLoading ? <option value="">loading...</option> : null}
            {harnessesQuery.data?.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="model">
          <Select
            disabled={!effectiveHarness || modelsQuery.isLoading || Boolean(modelsQuery.error)}
            onChange={(event) => setModelId(event.target.value)}
            value={effectiveModelId}
          >
            {!effectiveHarness ? <option value="">choose harness first</option> : null}
            {modelsQuery.isLoading ? <option value="">loading...</option> : null}
            {modelsQuery.data?.models.map((item) => (
              <option key={item.model_ref} value={item.model_ref}>
                {item.provider_name} / {item.model_name}
              </option>
            ))}
          </Select>
        </Field>

        {createSessionMutation.error ? (
          <div className="rounded-md border border-[rgba(239,68,68,0.34)] bg-[var(--danger-soft)] px-4 py-3 text-sm text-[var(--danger)]">
            {getErrorMessage(createSessionMutation.error)}
          </div>
        ) : null}
      </div>
    </Dialog>
  );
}

interface FieldProps {
  children: ReactNode;
  label: string;
}

function Field({ children, label }: FieldProps) {
  return (
    <label className="grid gap-2">
      <span className="text-sm text-[var(--muted)]">{label}</span>
      {children}
    </label>
  );
}
