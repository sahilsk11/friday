import { v4 as uuidv4 } from 'uuid';
import { createRequire } from 'module';
import type { AgentAdapter, AgentEventHandlers } from './types.js';

const require = createRequire(import.meta.url);
const { EventSource } = require('eventsource');

interface OpenCodeEvent {
  type: string;
  properties?: {
    sessionID: string;
    messageID?: string;
    partID?: string;
    field?: string;
    delta?: string;
    status?: { type: string };
    info?: {
      id: string;
      role?: string;
      finish?: string;
      time?: { completed?: number };
    };
    part?: {
      id: string;
      type: string;
      messageID?: string;
      tool?: string;
      callID?: string;
      state?: {
        status: string;
        input?: Record<string, unknown>;
        output?: string;
      };
    };
  };
}

export class OpenCodeAdapter implements AgentAdapter {
  private baseUrl: string;
  private subscriptions = new Map<string, { abort: AbortController; es: EventSource }>();
  // Track partID → part type so we can filter deltas. Opencode streams
  // 'reasoning' parts and 'text' parts both as field=text deltas — without
  // this map we'd forward the model's chain-of-thought to TTS along with
  // the actual answer. Cleared per session on subscribe.
  // sessionID → (partID → part type). Lets us filter deltas for 'reasoning'
  // parts so we don't forward the model's chain-of-thought to TTS.
  private partTypes = new Map<string, Map<string, string>>();
  // sessionID → (callID → tool status) for tool-event transition detection.
  private toolStates = new Map<string, Map<string, string>>();

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
  }

  async createSession(input?: { title?: string }): Promise<{ sessionId: string }> {
    const response = await fetch(`${this.baseUrl}/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: input?.title }),
    });

    if (!response.ok) {
      throw new Error(`Failed to create session: ${response.statusText}`);
    }

    const session = (await response.json()) as { id: string };
    return { sessionId: session.id };
  }

  async resumeSession(sessionId: string): Promise<{ sessionId: string }> {
    const response = await fetch(`${this.baseUrl}/session/${sessionId}`);
    if (!response.ok) {
      throw new Error(`Failed to resume session: ${response.statusText}`);
    }
    return { sessionId };
  }

  async sendTurn(sessionId: string, text: string): Promise<{ turnId: string }> {
    const turnId = uuidv4();

    const response = await fetch(`${this.baseUrl}/session/${sessionId}/prompt_async`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        parts: [{ type: 'text', text }],
      }),
    });

    if (!response.ok) {
      throw new Error(`Failed to send turn: ${response.statusText}`);
    }

    return { turnId };
  }

  async cancelTurn(sessionId: string, _turnId?: string): Promise<void> {
    const response = await fetch(`${this.baseUrl}/session/${sessionId}/abort`, {
      method: 'POST',
    });

    if (!response.ok) {
      throw new Error(`Failed to abort session: ${response.statusText}`);
    }
  }

  async subscribe(
    sessionId: string,
    handlers: AgentEventHandlers,
    _clientSessionId: string
  ): Promise<() => Promise<void>> {
    const abortController = new AbortController();
    const eventSource = new EventSource(`${this.baseUrl}/event`);

    this.subscriptions.set(sessionId, { abort: abortController, es: eventSource });
    this.partTypes.set(sessionId, new Map());
    this.toolStates.set(sessionId, new Map());

    eventSource.onmessage = (event: MessageEvent) => {
      if (abortController.signal.aborted) return;

      try {
        const data: OpenCodeEvent = JSON.parse(event.data);
        this.handleEvent(sessionId, data, handlers);
      } catch (error) {
        // Skip parse errors
      }
    };

    eventSource.onerror = () => {
      if (!abortController.signal.aborted) {
        handlers.onError?.(new Error('EventSource connection error'));
      }
    };

    return async () => {
      abortController.abort();
      eventSource.close();
      this.subscriptions.delete(sessionId);
      this.partTypes.delete(sessionId);
      this.toolStates.delete(sessionId);
    };
  }

  private handleEvent(
    sessionId: string,
    event: OpenCodeEvent,
    handlers: AgentEventHandlers
  ): void {
    const props = event.properties;
    if (!props || props.sessionID !== sessionId) {
      return;
    }

    switch (event.type) {
      case 'message.part.updated':
        if (props.part?.id && props.part.type) {
          this.partTypes.get(sessionId)?.set(props.part.id, props.part.type);
        }
        if (
          props.part?.type === 'tool' &&
          props.part.callID &&
          props.part.tool &&
          props.part.state
        ) {
          this.handleToolEvent(
            sessionId,
            {
              callID: props.part.callID,
              tool: props.part.tool,
              state: props.part.state,
            },
            handlers
          );
        }
        break;

      case 'message.part.delta':
        if (props.field === 'text' && props.delta && props.partID) {
          const partType = this.partTypes.get(sessionId)?.get(props.partID);
          if (partType === 'text') {
            handlers.onTextDelta(props.delta, props.messageID);
            handlers.onState?.('running');
          }
        }
        break;

      case 'message.updated':
        if (props.info?.finish === 'stop') {
          handlers.onState?.('done');
        }
        break;

      case 'session.status':
        if (props.status?.type === 'busy') {
          handlers.onState?.('running');
        } else if (props.status?.type === 'idle') {
          handlers.onState?.('idle');
        }
        break;

      case 'session.idle':
        handlers.onState?.('idle');
        break;

      default:
        break;
    }
  }

  private handleToolEvent(
    sessionId: string,
    part: { callID: string; tool: string; state?: { status: string; input?: Record<string, unknown> } },
    handlers: AgentEventHandlers
  ): void {
    const toolStates = this.toolStates.get(sessionId);
    if (!toolStates) return;

    const callID = part.callID;
    const newStatus = part.state?.status;
    if (!newStatus) return;

    const prevStatus = toolStates.get(callID);
    if (prevStatus === newStatus) return;

    toolStates.set(callID, newStatus);

    if (!handlers.onToolEvent) return;

    const toolName = part.tool || 'unknown';
    const input = part.state?.input;

    let phase: 'start' | 'update' | 'end';
    let message: string | undefined;

    if (newStatus === 'pending') {
      phase = 'start';
      message = `Starting ${toolName}...`;
    } else if (newStatus === 'running') {
      phase = 'update';
      const inputDesc = input ? this.summarizeInput(toolName, input) : '';
      message = inputDesc ? `Running ${toolName} on ${inputDesc}...` : `Running ${toolName}...`;
    } else if (newStatus === 'completed') {
      phase = 'end';
      message = `Finished ${toolName}`;
    } else if (newStatus === 'error') {
      phase = 'end';
      message = `Error in ${toolName}`;
    } else {
      return;
    }

    handlers.onToolEvent({ phase, toolName, message });
  }

  private summarizeInput(toolName: string, input: Record<string, unknown>): string {
    if (toolName === 'read' && input.filePath) {
      return String(input.filePath);
    }
    if (toolName === 'glob' && input.pattern) {
      return String(input.pattern);
    }
    if (toolName === 'bash' && input.command) {
      const cmd = String(input.command);
      return cmd.length > 40 ? cmd.slice(0, 40) + '...' : cmd;
    }
    if (toolName === 'edit' && input.filePath) {
      return String(input.filePath);
    }
    return '';
  }
}