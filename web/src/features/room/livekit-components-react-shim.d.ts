declare module '@livekit/components-react' {
  import type * as React from 'react';
  import type {
    ConnectionState,
    DataPublishOptions,
    DisconnectReason,
    LocalParticipant,
    Room,
    Track,
  } from 'livekit-client';
  import type { TrackReferenceOrPlaceholder } from '@livekit/components-core';

  export interface LiveKitRoomProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'onError'> {
    audio?: boolean;
    connect?: boolean;
    room?: Room;
    serverUrl: string | undefined;
    token: string | undefined;
    onConnected?: () => void;
    onDisconnected?: (reason?: DisconnectReason) => void;
    onError?: (error: Error) => void;
    onMediaDeviceFailure?: (failure?: unknown, kind?: string) => void;
  }

  export const LiveKitRoom: React.ForwardRefExoticComponent<
    React.PropsWithoutRef<React.PropsWithChildren<LiveKitRoomProps>> &
      React.RefAttributes<HTMLDivElement>
  >;

  export interface RoomAudioRendererProps {
    muted?: boolean;
    room?: Room;
    volume?: number;
  }

  export function RoomAudioRenderer(props: RoomAudioRendererProps): React.JSX.Element;

  export function useRoomContext(): Room;

  export function useConnectionState(room?: Room): ConnectionState;

  export function useLocalParticipant(options?: { room?: Room }): {
    isMicrophoneEnabled: boolean;
    isScreenShareEnabled: boolean;
    isCameraEnabled: boolean;
    microphoneTrack: unknown;
    cameraTrack: unknown;
    lastMicrophoneError: Error | undefined;
    lastCameraError: Error | undefined;
    localParticipant: LocalParticipant;
  };

  export interface ReceivedDataMessage<T extends string | undefined = string | undefined> {
    from?: {
      identity: string;
      name?: string;
    };
    payload: Uint8Array;
    topic?: T;
  }

  export function useDataChannel<T extends string>(
    topic: T,
    onMessage?: (message: ReceivedDataMessage<T>) => void,
  ): {
    isSending: boolean;
    message: ReceivedDataMessage<T> | undefined;
    send: (payload: Uint8Array, options: DataPublishOptions) => Promise<void>;
  };

  export function useDataChannel(
    onMessage?: (message: ReceivedDataMessage) => void,
  ): {
    isSending: boolean;
    message: ReceivedDataMessage | undefined;
    send: (payload: Uint8Array, options: DataPublishOptions) => Promise<void>;
  };

  export interface BarVisualizerProps extends React.HTMLProps<HTMLDivElement> {
    barCount?: number;
    options?: {
      maxHeight?: number;
      minHeight?: number;
    };
    state?: 'connecting' | 'initializing' | 'listening' | 'thinking';
    trackRef?: TrackReferenceOrPlaceholder;
  }

  export const BarVisualizer: React.ForwardRefExoticComponent<
    React.PropsWithoutRef<BarVisualizerProps> & React.RefAttributes<HTMLDivElement>
  >;

  export interface StartAudioProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
    label: string;
    room?: Room;
  }

  export const StartAudio: React.ForwardRefExoticComponent<
    React.PropsWithoutRef<StartAudioProps> & React.RefAttributes<HTMLButtonElement>
  >;

  export interface TrackToggleProps
    extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'onChange'> {
    onChange?: (enabled: boolean, isUserInitiated: boolean) => void;
    onDeviceError?: (error: Error) => void;
    showIcon?: boolean;
    source: Track.Source;
  }

  export const TrackToggle: React.ForwardRefExoticComponent<
    React.PropsWithoutRef<TrackToggleProps> & React.RefAttributes<HTMLButtonElement>
  >;
}
