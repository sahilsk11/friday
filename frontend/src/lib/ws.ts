import type { ClientMessage, ServerMessage } from '@/protocol.ts';

export interface GatewayConnection {
  send(msg: ClientMessage): void;
  close(): void;
  readyState(): number;
}

export interface ConnectGatewayOpts {
  onMessage(msg: ServerMessage): void;
  onOpen?(): void;
  onClose?(): void;
  onError?(err: Event): void;
}

// Opens a WebSocket to the voice gateway via the Vite proxy path /ws.
// The origin-relative URL means no hardcoded host — works in dev (proxy)
// and production (same origin) without changes.
export function connectGateway(opts: ConnectGatewayOpts): GatewayConnection {
  const ws = new WebSocket('/ws');

  ws.onopen = () => {
    opts.onOpen?.();
  };

  ws.onclose = () => {
    opts.onClose?.();
  };

  ws.onerror = (err: Event) => {
    opts.onError?.(err);
  };

  ws.onmessage = (event: MessageEvent) => {
    let msg: ServerMessage;
    try {
      msg = JSON.parse(event.data as string) as ServerMessage;
    } catch {
      console.error('Failed to parse server message', event.data);
      return;
    }
    opts.onMessage(msg);
  };

  return {
    send(msg: ClientMessage): void {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(msg));
      } else {
        console.warn('WebSocket not open, dropping message', msg.type);
      }
    },
    close(): void {
      ws.close();
    },
    readyState(): number {
      return ws.readyState;
    },
  };
}
