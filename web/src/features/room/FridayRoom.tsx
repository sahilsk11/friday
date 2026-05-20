import type { TrackReferenceOrPlaceholder } from "@livekit/components-core";
import {
  BarVisualizer,
  LiveKitRoom,
  RoomAudioRenderer,
  TrackToggle,
  useConnectionState,
  useDataChannel,
  useLocalParticipant,
  useRoomContext,
} from "@livekit/components-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  ConnectionState,
  ParticipantKind,
  RoomEvent,
  Track,
  type RemoteTrackPublication,
  type DisconnectReason,
  type LocalParticipant,
  type Participant,
  type RemoteParticipant,
  type TrackPublication,
} from "livekit-client";
import { ChevronDown, Send } from "lucide-react";

import "./friday-room.css";
import {
  ensureVoiceAgent,
  listNarratorEvents,
  submitNarratorTurn,
} from "../sessions/api";
import { getErrorMessage } from "../../lib/api";
import type { NarratorEventResponse, TranscriptEntry } from "../../types/api";

const AGENT_RESPONSE_TOPIC = "friday.agent_response";
const AGENT_DISPATCH_INITIAL_DELAY_MS = 10_000;
const AGENT_DISPATCH_RETRY_MS = 10_000;
const AGENT_JOIN_WARNING_MS = 20_000;
const TURN_CONTROL_RPC_METHODS = {
  cancel_turn: "friday.turn.cancel",
  end_turn: "friday.turn.end",
  set_speaker: "friday.turn.set_speaker",
  start_turn: "friday.turn.start",
  submit_text: "friday.turn.submit_text",
} as const satisfies Record<TurnControlType, string>;
const MAX_ACTIVITY_ENTRIES = 32;
const VOICE_PIPELINE_STEPS: Array<{
  description: string;
  id: VoicePipelineStepId;
  label: string;
}> = [
  {
    description: "Microphone capture",
    id: "capture",
    label: "Capture",
  },
  {
    description: "Turn control sent",
    id: "send",
    label: "Send",
  },
  {
    description: "STT and provider",
    id: "process",
    label: "Process",
  },
  {
    description: "Friday response",
    id: "reply",
    label: "Reply",
  },
];
const textDecoder = new TextDecoder();
const timeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
});

type TurnControlType =
  | "start_turn"
  | "end_turn"
  | "cancel_turn"
  | "set_speaker"
  | "submit_text";

export type FridayRoomSessionPayload = {
  expires_in_seconds: number;
  livekit_url: string;
  participant_identity: string;
  participant_name: string;
  room_name: string;
  session_id: string;
  token: string;
};

export type FridayRoomActivityEntry = {
  body?: string;
  id: string;
  time: string;
  title: string;
};

export type FridayRoomProps = {
  autoConnect?: boolean;
  className?: string;
  narratorTranscript?: TranscriptEntry[];
  onLeave?: () => void;
  providerLabel?: string;
  providerTranscript?: TranscriptEntry[];
  session: FridayRoomSessionPayload | null;
};

type RemoteParticipantSummary = {
  audioTrackCount: number;
  id: string;
  identity: string;
  isSpeaking: boolean;
  name: string;
  subscribedAudioCount: number;
};

type ActivityInput = Omit<FridayRoomActivityEntry, "id" | "time">;
type ConversationRole = "user" | "friday" | "system";
type ConversationEntry = {
  id: string;
  kind: string;
  role: ConversationRole;
  text: string;
};
type ConversationTab = "narrator" | "provider";
type VoicePipelineStage =
  | "idle"
  | "starting"
  | "listening"
  | "sending"
  | "transcribing"
  | "thinking"
  | "speaking"
  | "complete"
  | "error";
type VoicePipelineState = {
  detail?: string;
  stage: VoicePipelineStage;
  updatedAt: number;
};
type VoicePipelineStepId = "capture" | "send" | "process" | "reply";
type VoicePipelineStepState = "idle" | "active" | "done" | "error";
type AgentResponseMessage = {
  event_id?: number;
  message?: string;
  name?: string;
  state?: string;
  text?: string;
  type: string;
};
type TurnControlRpcResult = {
  ok: boolean;
  message?: string;
  state?: string;
  transcript?: string;
  type?: TurnControlType;
};

export function FridayRoom({
  autoConnect = true,
  className,
  narratorTranscript = [],
  onLeave,
  providerLabel = "provider",
  providerTranscript = [],
  session,
}: FridayRoomProps) {
  const [, setActivity] = useState<FridayRoomActivityEntry[]>([]);
  const [connectionRequested, setConnectionRequested] = useState(autoConnect);

  const addActivity = useCallback((entry: ActivityInput) => {
    setActivity((current) =>
      [
        {
          ...entry,
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          time: timeFormatter.format(new Date()),
        },
        ...current,
      ].slice(0, MAX_ACTIVITY_ENTRIES),
    );
  }, []);

  useEffect(() => {
    if (!session) {
      setActivity([]);
      return;
    }

    setConnectionRequested(autoConnect);
    setActivity([
      {
        id: `${Date.now()}-session`,
        time: timeFormatter.format(new Date()),
        title: "Session payload ready",
        body: `Room ${session.room_name} for ${session.participant_name || session.participant_identity}.`,
      },
    ]);
  }, [autoConnect, session]);

  if (!session) {
    return (
      <section
        className={joinClassNames("friday-room friday-room__empty", className)}
      >
        <div className="friday-room__card friday-room__empty-card">
          <h1>Waiting for session payload</h1>
          <p>LiveKit join details will appear here when the room is ready.</p>
        </div>
      </section>
    );
  }

  return (
    <LiveKitRoom
      audio={false}
      className={joinClassNames("friday-room", className)}
      connect={connectionRequested}
      onConnected={() => {
        addActivity({
          title: "Connected to LiveKit",
          body: `Joined ${session.room_name} as ${session.participant_identity}.`,
        });
      }}
      onDisconnected={(reason?: DisconnectReason) => {
        addActivity({
          title: "Disconnected",
          body: reason
            ? `Disconnect reason: ${String(reason)}.`
            : "Room connection closed.",
        });
      }}
      onError={(error: Error) => {
        addActivity({
          title: "Room error",
          body: error.message,
        });
      }}
      onMediaDeviceFailure={(_failure: unknown, kind?: string) => {
        addActivity({
          title: "Media device failure",
          body: kind
            ? `${kind} access failed.`
            : "Unable to access the selected media device.",
        });
      }}
      serverUrl={session.livekit_url}
      token={session.token}
    >
      <FridayRoomLayout
        onLeave={onLeave}
        narratorTranscript={narratorTranscript}
        onConnectionRequestedChange={setConnectionRequested}
        onLog={addActivity}
        providerLabel={providerLabel}
        providerTranscript={providerTranscript}
        session={session}
      />
    </LiveKitRoom>
  );
}

function FridayRoomLayout({
  narratorTranscript,
  onConnectionRequestedChange,
  onLeave,
  onLog,
  providerLabel,
  providerTranscript,
  session,
}: {
  narratorTranscript: TranscriptEntry[];
  onConnectionRequestedChange: (requested: boolean) => void;
  onLeave?: () => void;
  onLog: (entry: ActivityInput) => void;
  providerLabel: string;
  providerTranscript: TranscriptEntry[];
  session: FridayRoomSessionPayload;
}) {
  const room = useRoomContext();
  const connectionState = useConnectionState(room);
  const {
    isMicrophoneEnabled,
    lastMicrophoneError,
    localParticipant,
    microphoneTrack,
  } = useLocalParticipant();
  const [isHolding, setIsHolding] = useState(false);
  const [turnControlPending, setTurnControlPending] = useState<
    "starting" | "ending" | null
  >(null);
  const [audioPlaybackReady, setAudioPlaybackReady] = useState(false);
  const [activeConversationTab, setActiveConversationTab] =
    useState<ConversationTab>("narrator");
  const [conversation, setConversation] = useState<ConversationEntry[]>([]);
  const [draftText, setDraftText] = useState("");
  const [isCommandPending, setIsCommandPending] = useState(false);
  const [isSubmittingText, setIsSubmittingText] = useState(false);
  const [speakerEnabled, setSpeakerEnabled] = useState(true);
  const [voicePipeline, setVoicePipeline] = useState<VoicePipelineState>(() => ({
    detail: "No active turn.",
    stage: "idle",
    updatedAt: Date.now(),
  }));
  const [remoteParticipants, setRemoteParticipants] = useState<
    RemoteParticipantSummary[]
  >(() =>
    summarizeRemoteParticipants(
      room.remoteParticipants.values(),
      room.activeSpeakers,
    ),
  );
  const primaryAgent = remoteParticipants[0] ?? null;
  const primaryAgentIdentity = primaryAgent?.identity;
  const hasAgentParticipant = Boolean(primaryAgent);
  const narratorEventCursorRef = useRef(0);
  const seenNarratorEventIdsRef = useRef(new Set<number>());
  const narratorFeedRef = useRef<HTMLDivElement | null>(null);
  const providerFeedRef = useRef<HTMLDivElement | null>(null);

  const setVoiceStage = useCallback(
    (stage: VoicePipelineStage, detail?: string) => {
      setVoicePipeline({
        detail,
        stage,
        updatedAt: Date.now(),
      });
    },
    [],
  );

  useEffect(() => {
    setVoiceStage("idle", "No active turn.");
  }, [session.session_id, setVoiceStage]);

  useEffect(() => {
    setConversation(
      narratorTranscript
        .filter((entry) => entry.text.trim().length > 0)
        .map((entry, index) => ({
          id: `${session.session_id}-history-${index}`,
          kind: "history",
          role: entry.role === "user" ? "user" : "friday",
          text: entry.text.trim(),
        })),
    );
  }, [narratorTranscript, session.session_id]);

  useLayoutEffect(() => {
    const feed = narratorFeedRef.current;
    if (!feed) {
      return;
    }

    feed.scrollTop = feed.scrollHeight;
  }, [activeConversationTab, conversation]);

  useLayoutEffect(() => {
    const feed = providerFeedRef.current;
    if (!feed) {
      return;
    }

    feed.scrollTop = feed.scrollHeight;
  }, [activeConversationTab, providerTranscript]);

  const addConversationEntry = useCallback(
    (
      entry: Omit<ConversationEntry, "id">,
      options: { id?: string } = {},
    ) => {
      const id =
        options.id ?? `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      setConversation((current) => {
        if (current.some((row) => row.id === id)) {
          return current;
        }
        return [
          ...current,
          {
            ...entry,
            id,
          },
        ];
      });
    },
    [],
  );

  const markNarratorEventSeen = useCallback((eventId: number | undefined) => {
    if (typeof eventId !== "number") {
      return true;
    }
    narratorEventCursorRef.current = Math.max(
      narratorEventCursorRef.current,
      eventId,
    );
    if (seenNarratorEventIdsRef.current.has(eventId)) {
      return false;
    }
    seenNarratorEventIdsRef.current.add(eventId);
    return true;
  }, []);

  const addNarratorEvent = useCallback(
    (event: NarratorEventResponse) => {
      narratorEventCursorRef.current = Math.max(
        narratorEventCursorRef.current,
        event.id,
      );
      if (seenNarratorEventIdsRef.current.has(event.id)) {
        return;
      }
      seenNarratorEventIdsRef.current.add(event.id);

      if (event.type === "state") {
        const nextState = voicePipelineForAgentState(
          stringValue(event.payload?.state),
        );
        if (nextState) {
          setVoiceStage(nextState.stage, nextState.detail);
        }
        return;
      }

      const text = event.text?.trim();
      if (!text) {
        return;
      }

      if (event.type === "speech" || event.type === "progress") {
        setVoiceStage("thinking", "Friday is still working on the turn.");
        addConversationEntry(
          { kind: "narration", role: "friday", text },
          { id: `narrator-event-${event.id}` },
        );
        return;
      }

      if (event.type === "final") {
        setVoiceStage("complete", "Friday sent a response.");
        addConversationEntry(
          { kind: "text_final", role: "friday", text },
          { id: `narrator-event-${event.id}` },
        );
        return;
      }

      if (event.type === "error") {
        setVoiceStage("error", text);
        addConversationEntry(
          { kind: "error", role: "system", text },
          { id: `narrator-event-${event.id}` },
        );
      }
    },
    [addConversationEntry, setVoiceStage],
  );

  const handleDataMessage = useCallback(
    (message: {
      from?: { identity: string; name?: string };
      payload: Uint8Array;
      topic?: string;
    }) => {
      const parsed = parseAgentResponseMessage(message.payload);
      if (!parsed) {
        return;
      }

      const text = getAgentResponseText(parsed);
      if (!markNarratorEventSeen(parsed.event_id)) {
        return;
      }

      console.info("[Friday voice] agent response", {
        eventId: parsed.event_id,
        hasText: Boolean(text),
        state: parsed.state,
        type: parsed.type,
      });

      const eventEntryOptions: { id?: string } =
        typeof parsed.event_id === "number"
          ? { id: `narrator-event-${parsed.event_id}` }
          : {};

      if (parsed.type === "state") {
        const nextState = voicePipelineForAgentState(parsed.state ?? text ?? "");
        if (nextState) {
          setVoiceStage(nextState.stage, nextState.detail);
        }
        return;
      }

      if (parsed.type === "transcript" && text) {
        setVoiceStage("thinking", "Transcript committed. Friday is working.");
        addConversationEntry(
          { kind: parsed.type, role: "user", text },
          eventEntryOptions,
        );
        return;
      }

      if (parsed.type === "narration" && text) {
        setVoiceStage("thinking", "Friday is still working on the turn.");
        addConversationEntry(
          { kind: parsed.type, role: "friday", text },
          eventEntryOptions,
        );
        return;
      }

      if (parsed.type === "text_final" && text) {
        setVoiceStage("complete", "Friday sent a response.");
        addConversationEntry(
          { kind: parsed.type, role: "friday", text },
          eventEntryOptions,
        );
        return;
      }

      if (parsed.type === "error") {
        setVoiceStage(
          "error",
          text || "Friday hit an error while handling the turn.",
        );
        addConversationEntry(
          {
            kind: parsed.type,
            role: "system",
            text: text || "Friday hit an error while handling the turn.",
          },
          eventEntryOptions,
        );
      }
    },
    [addConversationEntry, markNarratorEventSeen, setVoiceStage],
  );

  useDataChannel(AGENT_RESPONSE_TOPIC, handleDataMessage);

  const callTurnControlRpc = useCallback(
    async (
      type: TurnControlType,
      options: { speakerEnabled?: boolean; text?: string } = {},
    ) => {
      if (!primaryAgentIdentity) {
        throw new Error("Friday agent is not connected.");
      }

      setIsCommandPending(true);
      try {
        const response = await localParticipant.performRpc({
          destinationIdentity: primaryAgentIdentity,
          method: TURN_CONTROL_RPC_METHODS[type],
          payload: encodeTurnControlPayload(type, options),
          responseTimeout: type === "end_turn" ? 20_000 : 10_000,
        });
        const result = parseTurnControlRpcResult(response);
        if (!result.ok) {
          throw new Error(
            result.message || "Friday could not handle the turn command.",
          );
        }
        return result;
      } finally {
        setIsCommandPending(false);
      }
    },
    [localParticipant, primaryAgentIdentity],
  );

  useEffect(() => {
    let cancelled = false;
    let pollTimer: number | undefined;

    const poll = async (bootstrap = false) => {
      try {
        const response = await listNarratorEvents(session.session_id, {
          afterId: bootstrap ? 0 : narratorEventCursorRef.current,
          limit: 100,
        });
        if (cancelled) {
          return;
        }
        if (bootstrap) {
          for (const event of response.events) {
            narratorEventCursorRef.current = Math.max(
              narratorEventCursorRef.current,
              event.id,
            );
            seenNarratorEventIdsRef.current.add(event.id);
          }
          return;
        }
        for (const event of response.events) {
          addNarratorEvent(event);
        }
      } catch (error) {
        if (!cancelled) {
          onLog({
            title: "Unable to poll narrator events",
            body:
              error instanceof Error
                ? error.message
                : "The event stream could not be refreshed.",
          });
        }
      }
    };

    void poll(true).finally(() => {
      if (cancelled) {
        return;
      }
      pollTimer = window.setInterval(() => {
        void poll(false);
      }, 1000);
    });

    return () => {
      cancelled = true;
      if (pollTimer !== undefined) {
        window.clearInterval(pollTimer);
      }
    };
  }, [addNarratorEvent, onLog, session.session_id]);

  const refreshParticipants = useCallback(() => {
    setRemoteParticipants(
      summarizeRemoteParticipants(
        room.remoteParticipants.values(),
        room.activeSpeakers,
      ),
    );
  }, [room]);

  useEffect(() => {
    for (const participant of room.remoteParticipants.values()) {
      for (const publication of participant.audioTrackPublications.values()) {
        (publication as RemoteTrackPublication).setEnabled(speakerEnabled);
      }
    }
  }, [remoteParticipants, room, speakerEnabled]);

  useEffect(() => {
    refreshParticipants();

    const handleParticipantConnected = (participant: RemoteParticipant) => {
      refreshParticipants();
      onLog({
        title: "Participant connected",
        body: formatParticipantLabel(participant),
      });
    };

    const handleParticipantDisconnected = (participant: RemoteParticipant) => {
      refreshParticipants();
      onLog({
        title: "Participant disconnected",
        body: formatParticipantLabel(participant),
      });
    };

    const handleTrackSubscribed = (
      track: Track,
      _publication: TrackPublication,
      participant: RemoteParticipant,
    ) => {
      refreshParticipants();
      if (track.kind === Track.Kind.Audio) {
        onLog({
          title: "Agent audio subscribed",
          body: formatParticipantLabel(participant),
        });
      }
    };

    const handleTrackUnsubscribed = (
      track: Track,
      _publication: TrackPublication,
      participant: RemoteParticipant,
    ) => {
      refreshParticipants();
      if (track.kind === Track.Kind.Audio) {
        onLog({
          title: "Agent audio unsubscribed",
          body: formatParticipantLabel(participant),
        });
      }
    };

    const handlePlaybackChanged = (playing: boolean) => {
      setAudioPlaybackReady(playing);
      onLog({
        title: playing ? "Agent audio active" : "Agent audio idle",
        body: playing
          ? "Remote audio is playing through the room renderer."
          : "No remote audio is currently playing.",
      });
    };

    const handleActiveSpeakersChanged = (speakers: Participant[]) => {
      setRemoteParticipants(
        summarizeRemoteParticipants(room.remoteParticipants.values(), speakers),
      );
    };

    room.on(RoomEvent.ParticipantConnected, handleParticipantConnected);
    room.on(RoomEvent.ParticipantDisconnected, handleParticipantDisconnected);
    room.on(RoomEvent.TrackSubscribed, handleTrackSubscribed);
    room.on(RoomEvent.TrackUnsubscribed, handleTrackUnsubscribed);
    room.on(RoomEvent.AudioPlaybackStatusChanged, handlePlaybackChanged);
    room.on(RoomEvent.ActiveSpeakersChanged, handleActiveSpeakersChanged);

    return () => {
      room.off(RoomEvent.ParticipantConnected, handleParticipantConnected);
      room.off(
        RoomEvent.ParticipantDisconnected,
        handleParticipantDisconnected,
      );
      room.off(RoomEvent.TrackSubscribed, handleTrackSubscribed);
      room.off(RoomEvent.TrackUnsubscribed, handleTrackUnsubscribed);
      room.off(RoomEvent.AudioPlaybackStatusChanged, handlePlaybackChanged);
      room.off(RoomEvent.ActiveSpeakersChanged, handleActiveSpeakersChanged);
    };
  }, [onLog, refreshParticipants, room]);

  useEffect(() => {
    if (connectionState !== ConnectionState.Connected) {
      setIsHolding(false);
      setTurnControlPending(null);
      setVoiceStage(
        "idle",
        connectionState === ConnectionState.Disconnected
          ? "Voice is disconnected."
          : "Connecting to the room.",
      );
    }
  }, [connectionState, setVoiceStage]);

  useEffect(() => {
    if (
      connectionState === ConnectionState.Connected &&
      hasAgentParticipant &&
      voicePipeline.stage === "starting"
    ) {
      setVoiceStage("idle", "Friday agent is ready.");
    }
  }, [
    connectionState,
    hasAgentParticipant,
    setVoiceStage,
    voicePipeline.stage,
  ]);

  useEffect(() => {
    if (connectionState !== ConnectionState.Connected) {
      return;
    }

    if (hasAgentParticipant) {
      return;
    }

    let cancelled = false;
    let requestInFlight = false;
    let dispatchAttempts = 0;

    const requestDispatch = () => {
      if (cancelled || requestInFlight) {
        return;
      }

      requestInFlight = true;
      dispatchAttempts += 1;
      setVoiceStage("starting", "Asking the Friday agent to join this room.");
      onLog({
        title:
          dispatchAttempts === 1
            ? "Requesting agent dispatch"
            : "Retrying agent dispatch",
        body: `Room ${session.room_name}.`,
      });

      void ensureVoiceAgent(session.session_id, {
        room_name: session.room_name,
      })
        .then(() => {
          setVoiceStage(
            "starting",
            "Agent dispatch requested. Waiting for the Friday agent.",
          );
          onLog({
            title: "Agent dispatch requested",
            body: `Waiting for Friday to join ${session.room_name}.`,
          });
        })
        .catch((error: unknown) => {
          const message = getErrorMessage(error);
          setVoiceStage("error", message);
          onLog({
            title: "Agent dispatch failed",
            body: message,
          });
        })
        .finally(() => {
          requestInFlight = false;
        });
    };

    let dispatchInterval: number | undefined;
    const dispatchTimeout = window.setTimeout(() => {
      requestDispatch();
      dispatchInterval = window.setInterval(
        requestDispatch,
        AGENT_DISPATCH_RETRY_MS,
      );
    }, AGENT_DISPATCH_INITIAL_DELAY_MS);
    const joinTimeout = window.setTimeout(() => {
      setVoiceStage(
        "error",
        "Agent dispatch was requested, but Friday has not joined the room yet.",
      );
      onLog({
        title: "Agent still not connected",
        body: "Retrying while the room remains connected.",
      });
    }, AGENT_JOIN_WARNING_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(dispatchTimeout);
      if (dispatchInterval !== undefined) {
        window.clearInterval(dispatchInterval);
      }
      window.clearTimeout(joinTimeout);
    };
  }, [
    connectionState,
    hasAgentParticipant,
    onLog,
    session.room_name,
    session.session_id,
    setVoiceStage,
  ]);

  useEffect(() => {
    if (!lastMicrophoneError) {
      return;
    }

    onLog({
      title: "Microphone error",
      body: lastMicrophoneError.message,
    });
  }, [lastMicrophoneError, onLog]);

  const startTurn = useCallback(async () => {
    if (
      connectionState !== ConnectionState.Connected ||
      isHolding ||
      turnControlPending !== null
    ) {
      return;
    }

    if (!hasAgentParticipant) {
      setVoiceStage(
        "error",
        "Voice is connected, but the Friday agent is not in the room.",
      );
      onLog({
        title: "Agent not connected",
        body: "Voice turns are unavailable until the Friday agent joins the room.",
      });
      return;
    }

    try {
      setTurnControlPending("starting");
      setVoiceStage("starting", "Opening the microphone.");
      console.info("[Friday voice] start_turn requested");
      if (!isMicrophoneEnabled) {
        await localParticipant.setMicrophoneEnabled(true);
      }
      await waitForMicrophonePublication(localParticipant);
      await callTurnControlRpc("start_turn");
      setIsHolding(true);
      console.info("[Friday voice] start_turn sent");
      setVoiceStage("listening", "Friday is receiving microphone audio.");
      onLog({
        title: "Turn opened",
        body: "Friday is listening.",
      });
    } catch (error) {
      setIsHolding(false);
      setVoiceStage(
        "error",
        error instanceof Error
          ? error.message
          : "LiveKit rejected the turn start request.",
      );
      onLog({
        title: "Unable to start turn",
        body:
          error instanceof Error
            ? error.message
            : "LiveKit rejected the turn start request.",
      });
    } finally {
      setTurnControlPending(null);
    }
  }, [
    callTurnControlRpc,
    connectionState,
    hasAgentParticipant,
    isHolding,
    isMicrophoneEnabled,
    localParticipant,
    onLog,
    setVoiceStage,
    turnControlPending,
  ]);

  const endTurn = useCallback(async () => {
    if (
      connectionState !== ConnectionState.Connected ||
      !isHolding ||
      turnControlPending !== null
    ) {
      return;
    }

    try {
      setTurnControlPending("ending");
      setIsHolding(false);
      setVoiceStage("sending", "Sending the end-of-turn command.");
      console.info("[Friday voice] end_turn requested");
      const commit = callTurnControlRpc("end_turn");
      await localParticipant.setMicrophoneEnabled(false);
      await commit;
      console.info("[Friday voice] end_turn sent");
      setVoiceStage("transcribing", "Audio sent. Waiting for transcription.");
      onLog({
        title: "Turn sent",
        body: "Waiting for Friday to respond.",
      });
    } catch (error) {
      setVoiceStage(
        "error",
        error instanceof Error
          ? error.message
          : "LiveKit rejected the turn end request.",
      );
      onLog({
        title: "Unable to end turn",
        body:
          error instanceof Error
            ? error.message
            : "LiveKit rejected the turn end request.",
      });
    } finally {
      setTurnControlPending(null);
    }
  }, [
    callTurnControlRpc,
    connectionState,
    isHolding,
    localParticipant,
    onLog,
    setVoiceStage,
    turnControlPending,
  ]);

  const toggleTurn = useCallback(() => {
    if (isHolding) {
      void endTurn();
      return;
    }

    void startTurn();
  }, [endTurn, isHolding, startTurn]);

  const cancelTurn = useCallback(async () => {
    if (connectionState !== ConnectionState.Connected) {
      return;
    }

    try {
      setIsHolding(false);
      setTurnControlPending(null);
      setVoiceStage("idle", "Cancelling the active turn.");
      await callTurnControlRpc("cancel_turn");
      await localParticipant.setMicrophoneEnabled(false);
      onLog({
        title: "Turn cancelled",
        body: "The active request was cancelled.",
      });
    } catch (error) {
      setVoiceStage(
        "error",
        error instanceof Error
          ? error.message
          : "LiveKit rejected the turn cancel request.",
      );
      onLog({
        title: "Unable to cancel turn",
        body:
          error instanceof Error
            ? error.message
            : "LiveKit rejected the turn cancel request.",
      });
    }
  }, [
    callTurnControlRpc,
    connectionState,
    localParticipant,
    onLog,
    setVoiceStage,
  ]);

  const setSpeaker = useCallback(
    async (enabled: boolean) => {
      setSpeakerEnabled(enabled);
      if (enabled) {
        await room.startAudio().catch((error: unknown) => {
          onLog({
            title: "Unable to start audio",
            body:
              error instanceof Error
                ? error.message
                : "The browser blocked audio playback.",
          });
        });
      }
      if (connectionState !== ConnectionState.Connected) {
        return;
      }
      try {
        await callTurnControlRpc("set_speaker", { speakerEnabled: enabled });
      } catch (error) {
        onLog({
          title: "Unable to update speaker",
          body:
            error instanceof Error
              ? error.message
              : "LiveKit rejected the speaker setting.",
        });
      }
    },
    [callTurnControlRpc, connectionState, onLog, room],
  );

  useEffect(() => {
    if (
      connectionState !== ConnectionState.Connected ||
      !hasAgentParticipant
    ) {
      return;
    }
    void callTurnControlRpc("set_speaker", { speakerEnabled }).catch(
      (error: unknown) => {
        onLog({
          title: "Unable to sync speaker",
          body:
            error instanceof Error
              ? error.message
              : "LiveKit rejected the speaker setting.",
        });
      },
    );
  }, [
    callTurnControlRpc,
    connectionState,
    hasAgentParticipant,
    onLog,
    speakerEnabled,
  ]);

  const submitTextTurn = useCallback(async (mode: "agent" | "direct" = "agent") => {
    const text = draftText.trim();
    if (!text || isSubmittingText) {
      return;
    }

    if (
      mode === "agent" &&
      (connectionState !== ConnectionState.Connected || !hasAgentParticipant)
    ) {
      setVoiceStage(
        "error",
        "Text via agent is unavailable because the Friday agent is not connected.",
      );
      onLog({
        title: "Text via agent unavailable",
        body: "Use direct API only when intentionally bypassing LiveKit and the agent.",
      });
      return;
    }

    setDraftText("");
    setIsSubmittingText(true);
    setVoiceStage(
      "thinking",
      mode === "agent"
        ? "Text sent through LiveKit. Friday is working."
        : "Text sent directly to the narrator API.",
    );
    addConversationEntry({ kind: "text", role: "user", text });
    onLog({
      title: mode === "agent" ? "Text turn sent via agent" : "Direct API text turn",
      body:
        mode === "agent"
          ? "Sending text over LiveKit to the Friday agent."
          : "Bypassing LiveKit and sending text directly to the narrator API.",
    });

    try {
      if (mode === "agent") {
        console.info("[Friday text] submit_text requested");
        await callTurnControlRpc("submit_text", { text });
        console.info("[Friday text] submit_text sent");
      } else {
        const response = await submitNarratorTurn(session.session_id, {
          source: "text",
          text,
        });
        for (const event of response.events) {
          addNarratorEvent(event);
        }
      }
    } catch (error) {
      setVoiceStage(
        "error",
        error instanceof Error
          ? error.message
          : "Friday could not submit the text turn.",
      );
      addConversationEntry({
        kind: "error",
        role: "system",
        text:
          error instanceof Error
            ? error.message
            : "Friday could not submit the text turn.",
      });
    } finally {
      setIsSubmittingText(false);
    }
  }, [
    addConversationEntry,
    addNarratorEvent,
    callTurnControlRpc,
    connectionState,
    draftText,
    hasAgentParticipant,
    isSubmittingText,
    onLog,
    session.session_id,
    setVoiceStage,
  ]);

  const disconnectVoice = useCallback(async () => {
    setIsHolding(false);
    setTurnControlPending(null);
    setVoiceStage("idle", "Voice is disconnecting.");
    try {
      await localParticipant.setMicrophoneEnabled(false);
      onConnectionRequestedChange(false);
      await room.disconnect();
    } finally {
      onLeave?.();
    }
  }, [
    localParticipant,
    onConnectionRequestedChange,
    onLeave,
    room,
    setVoiceStage,
  ]);

  const reconnectVoice = useCallback(() => {
    onConnectionRequestedChange(true);
    setTurnControlPending(null);
    setVoiceStage("idle", "Reconnecting voice.");
    onLog({
      title: "Reconnecting voice",
      body: "Opening a new LiveKit room connection for this session.",
    });
  }, [onConnectionRequestedChange, onLog, setVoiceStage]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.repeat) {
        return;
      }
      if (isTextEntryTarget(event.target)) {
        return;
      }

      if (event.code === "Space") {
        event.preventDefault();
        void toggleTurn();
      }

      if (event.key === "Escape") {
        event.preventDefault();
        void cancelTurn();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [cancelTurn, toggleTurn]);

  const connectionTone =
    connectionState === ConnectionState.Connected
      ? "connected"
      : connectionState === ConnectionState.Disconnected
        ? "danger"
        : "warning";
  const agentStatusTone = primaryAgent
    ? "connected"
    : connectionState === ConnectionState.Connected
      ? "danger"
      : "warning";
  const agentStatusLabel = primaryAgent
    ? primaryAgent.isSpeaking
      ? "speaking"
      : "ready"
    : connectionState === ConnectionState.Connected
      ? "not connected"
      : "waiting";

  const roomStatus = getRoomStatus({
    audioPlaybackReady,
    connectionState,
    isHolding,
    isSending: isCommandPending,
    remoteParticipants,
  });
  const voiceStatus = getVoicePipelineStatus({
    connectionState,
    isHolding,
    primaryAgent,
    voicePipeline,
  });

  const micTrackRef: TrackReferenceOrPlaceholder = useMemo(
    () =>
      microphoneTrack
        ? {
            participant: localParticipant,
            publication: microphoneTrack as TrackPublication,
            source: Track.Source.Microphone,
          }
        : {
            participant: localParticipant,
            source: Track.Source.Microphone,
          },
    [localParticipant, microphoneTrack],
  );

  return (
    <section className="friday-room__shell">
      <RoomAudioRenderer muted={!speakerEnabled} volume={speakerEnabled ? 1 : 0} />
      <div className="friday-room__layout">
        <article className="friday-room__card friday-room__control">
          <dl className="friday-room__status-list">
            <div>
              <dt>Client</dt>
              <dd data-tone={connectionTone}>{connectionState}</dd>
            </div>
            <div>
              <dt>Agent</dt>
              <dd data-tone={agentStatusTone}>{agentStatusLabel}</dd>
            </div>
          </dl>

          <div className="friday-room__mic-panel">
            <BarVisualizer
              barCount={21}
              className="friday-room__visualizer"
              options={{ maxHeight: 88, minHeight: 14 }}
              state={
                isHolding
                  ? "listening"
                  : primaryAgent?.isSpeaking
                    ? "thinking"
                    : "connecting"
              }
              trackRef={micTrackRef}
            />
            <p>{isMicrophoneEnabled ? roomStatus.label : "mic muted"}</p>
          </div>

          <div
            aria-live="polite"
            className="friday-room__turn-status"
            data-stage={voiceStatus.stage}
          >
            <div className="friday-room__turn-status-copy">
              <span>Turn status</span>
              <strong>{voiceStatus.label}</strong>
              <p>{voiceStatus.description}</p>
            </div>
            <ol className="friday-room__turn-steps">
              {VOICE_PIPELINE_STEPS.map((step) => (
                <li
                  data-state={getVoicePipelineStepState(
                    voiceStatus.stage,
                    step.id,
                  )}
                  key={step.id}
                >
                  <span aria-hidden="true" />
                  <div>
                    <strong>{step.label}</strong>
                    <small>{step.description}</small>
                  </div>
                </li>
              ))}
            </ol>
          </div>

          <button
            aria-pressed={speakerEnabled}
            className="friday-room__button"
            onClick={() => void setSpeaker(!speakerEnabled)}
            type="button"
          >
            speaker: {speakerEnabled ? "on" : "off"}
          </button>

          <button
            aria-pressed={isHolding}
            className="friday-room__button friday-room__button--primary"
            disabled={
              connectionState !== ConnectionState.Connected ||
              !hasAgentParticipant ||
              turnControlPending !== null
            }
            onClick={toggleTurn}
            type="button"
          >
            {turnControlPending === "starting"
              ? "Starting..."
              : turnControlPending === "ending"
                ? "Sending..."
                : isHolding
                  ? "Send"
                  : "Start"}
          </button>

          <button
            className="friday-room__button friday-room__button--danger"
            disabled={connectionState !== ConnectionState.Connected}
            onClick={() => void cancelTurn()}
            type="button"
          >
            Interrupt
          </button>

          <div className="friday-room__bottom-controls">
            <TrackToggle
              className="friday-room__pill"
              onDeviceError={(error) => {
                onLog({
                  title: "Microphone error",
                  body: error.message,
                });
              }}
              showIcon={false}
              source={Track.Source.Microphone}
            >
              mic: {isMicrophoneEnabled ? "on" : "off"}
            </TrackToggle>
            <button
              className="friday-room__pill"
              disabled={
                connectionState === ConnectionState.Connecting ||
                connectionState === ConnectionState.Reconnecting
              }
              onClick={() => {
                if (connectionState === ConnectionState.Disconnected) {
                  reconnectVoice();
                  return;
                }
                void disconnectVoice();
              }}
              type="button"
            >
              {connectionState === ConnectionState.Disconnected
                ? "reconnect voice"
                : "disconnect voice"}
            </button>
          </div>
        </article>

        <aside className="friday-room__card friday-room__conversation">
          <div className="friday-room__activity-header">
            <div
              aria-label="Conversation view"
              className="friday-room__tabbar"
              role="tablist"
            >
              <button
                aria-selected={activeConversationTab === "narrator"}
                className="friday-room__tab"
                onClick={() => setActiveConversationTab("narrator")}
                role="tab"
                type="button"
              >
                Narrator
              </button>
              <button
                aria-selected={activeConversationTab === "provider"}
                className="friday-room__tab"
                onClick={() => setActiveConversationTab("provider")}
                role="tab"
                type="button"
              >
                {providerLabel}
              </button>
            </div>
            <span>{session.room_name}</span>
          </div>

          {activeConversationTab === "narrator" ? (
            <div
              className="friday-room__conversation-feed"
              ref={narratorFeedRef}
              role="tabpanel"
            >
              {conversation.length ? (
                conversation.map((entry) => (
                  <div
                    className="friday-room__feed-item"
                    data-kind={entry.kind}
                    data-role={entry.role}
                    key={entry.id}
                  >
                    <span className="friday-room__feed-role">
                      {formatConversationRole(entry.role)}
                    </span>
                    <p className="friday-room__feed-text">{entry.text}</p>
                  </div>
                ))
              ) : (
                <div className="friday-room__conversation-empty">
                  <p>{roomStatus.description}</p>
                </div>
              )}
            </div>
          ) : (
            <ProviderTranscriptFeed
              entries={providerTranscript}
              feedRef={providerFeedRef}
            />
          )}

          <div className="friday-room__diagnostics" aria-label="room diagnostics">
            <span>{voiceStatus.label}</span>
            {primaryAgent ? (
              <span>
                {primaryAgent.isSpeaking ? "speaking" : "idle"} ·{" "}
                {primaryAgent.subscribedAudioCount} audio subscribed
              </span>
            ) : null}
          </div>

          <form
            className="friday-room__composer"
            onSubmit={(event) => {
              event.preventDefault();
              void submitTextTurn("agent");
            }}
          >
            <textarea
              aria-label="Message Friday"
              className="friday-room__composer-input"
              disabled={isSubmittingText}
              onChange={(event) => setDraftText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submitTextTurn("agent");
                }
              }}
              placeholder="Type the same thing you would say..."
              rows={2}
              value={draftText}
            />
            <div className="friday-room__composer-actions">
              <button
                aria-label="Send via agent"
                className="friday-room__composer-send"
                disabled={
                  !draftText.trim() ||
                  isSubmittingText ||
                  connectionState !== ConnectionState.Connected ||
                  !hasAgentParticipant
                }
                title="Send via agent"
                type="submit"
              >
                <Send aria-hidden="true" size={18} strokeWidth={2.2} />
              </button>
              <details className="friday-room__composer-menu">
                <summary aria-label="Text send options" title="Text send options">
                  <ChevronDown aria-hidden="true" size={16} strokeWidth={2.2} />
                </summary>
                <div className="friday-room__composer-menu-popover" role="menu">
                  <button
                    disabled={!draftText.trim() || isSubmittingText}
                    onClick={() => void submitTextTurn("direct")}
                    role="menuitem"
                    type="button"
                  >
                    Direct API
                  </button>
                </div>
              </details>
            </div>
          </form>
        </aside>
      </div>
    </section>
  );
}

function ProviderTranscriptFeed({
  entries,
  feedRef,
}: {
  entries: TranscriptEntry[];
  feedRef: RefObject<HTMLDivElement | null>;
}) {
  const visibleEntries = entries.filter(hasProviderTranscriptContent);

  return (
    <div
      className="friday-room__conversation-feed friday-room__provider-feed"
      ref={feedRef}
      role="tabpanel"
    >
      {visibleEntries.length ? (
        visibleEntries.map((entry, index) => (
          <div
            className="friday-room__provider-entry"
            data-role={entry.role || "unknown"}
            key={`${entry.completed_at ?? "pending"}-${entry.role}-${index}`}
          >
            <div className="friday-room__provider-meta">
              <span>{formatProviderRole(entry.role)}</span>
              {entry.model ? (
                <span>
                  {entry.model.provider_id}/{entry.model.model_id}
                </span>
              ) : null}
              {entry.completed_at ? (
                <span>{formatTranscriptTime(entry.completed_at)}</span>
              ) : null}
            </div>

            {entry.error ? (
              <p className="friday-room__provider-error">{entry.error}</p>
            ) : null}

            {(entry.parts ?? []).length ? (
              <div className="friday-room__provider-parts">
                {visibleProviderParts(entry.parts ?? []).map((part, partIndex) => (
                  <ProviderTranscriptPart
                    key={`${index}-${partIndex}`}
                    part={part}
                    partIndex={partIndex}
                  />
                ))}
              </div>
            ) : (
              <p className="friday-room__provider-text">
                {entry.text.trim() || "(no text)"}
              </p>
            )}
          </div>
        ))
      ) : (
        <div className="friday-room__conversation-empty">
          <p>No provider transcript yet.</p>
        </div>
      )}
    </div>
  );
}

function ProviderTranscriptPart({
  part,
  partIndex,
}: {
  part: Record<string, unknown>;
  partIndex: number;
}) {
  const type = stringValue(part.type) || stringValue(part.kind) || "part";
  const state = recordValue(part.state);
  const text = stringValue(part.text) || stringValue(part.content);
  const toolName =
    stringValue(part.tool) ||
    stringValue(part.toolName) ||
    stringValue(part.tool_name) ||
    stringValue(part.name);
  const title = stringValue(part.title) || stringValue(state.title);
  const status =
    stringValue(part.status) ||
    stringValue(part.state) ||
    stringValue(state.status);
  const args =
    state.input ??
    part.input ??
    part.args ??
    part.arguments ??
    part.parameters ??
    undefined;
  const result =
    state.output ??
    part.output ??
    part.result ??
    part.response ??
    part.observation ??
    undefined;
  const hasStructuredDetails =
    toolName || title || status || args !== undefined || result !== undefined;

  if (type === "text" && text && !hasStructuredDetails) {
    return <p className="friday-room__provider-text">{text}</p>;
  }

  if (type === "text" && !text && !hasStructuredDetails) {
    return null;
  }

  if (type === "reasoning" && text) {
    return (
      <details className="friday-room__provider-part">
        <summary>thinking</summary>
        <p className="friday-room__provider-text">{text}</p>
      </details>
    );
  }

  return (
    <details className="friday-room__provider-part">
      <summary>
        {formatProviderPartSummary({
          partIndex,
          title,
          toolName,
          type,
        })}
        {status ? <span>{status}</span> : null}
      </summary>
      <div className="friday-room__provider-detail">
        {text ? <p>{text}</p> : null}
        {args !== undefined ? (
          <ProviderValue label="args" value={args} />
        ) : null}
        {result !== undefined ? (
          <ProviderValue label="result" value={result} />
        ) : null}
        <ProviderValue label="raw" value={part} />
      </div>
    </details>
  );
}

function ProviderValue({
  label,
  value,
}: {
  label: string;
  value: unknown;
}) {
  return (
    <div className="friday-room__provider-value">
      <span>{label}</span>
      <pre>{formatProviderValue(value)}</pre>
    </div>
  );
}

function getVoicePipelineStatus({
  connectionState,
  isHolding,
  primaryAgent,
  voicePipeline,
}: {
  connectionState: ConnectionState;
  isHolding: boolean;
  primaryAgent: RemoteParticipantSummary | null;
  voicePipeline: VoicePipelineState;
}): { description: string; label: string; stage: VoicePipelineStage } {
  if (voicePipeline.stage === "error") {
    return {
      description: voicePipeline.detail || "The last turn failed.",
      label: "needs attention",
      stage: "error",
    };
  }

  if (connectionState === ConnectionState.Disconnected) {
    return {
      description: "Voice is disconnected.",
      label: "voice disconnected",
      stage: "idle",
    };
  }

  if (
    connectionState === ConnectionState.Connecting ||
    connectionState === ConnectionState.Reconnecting
  ) {
    return {
      description: "Connecting to LiveKit.",
      label: "connecting",
      stage: "idle",
    };
  }

  if (isHolding) {
    return {
      description: "Microphone audio is live.",
      label: "listening",
      stage: "listening",
    };
  }

  if (
    primaryAgent?.isSpeaking &&
    (voicePipeline.stage === "thinking" ||
      voicePipeline.stage === "complete" ||
      voicePipeline.stage === "speaking")
  ) {
    return {
      description: "Friday is playing audio.",
      label: "speaking",
      stage: "speaking",
    };
  }

  if (voicePipeline.stage === "idle" && !primaryAgent) {
    return {
      description: "Voice is connected, but the Friday agent is not in the room.",
      label: "agent not connected",
      stage: "error",
    };
  }

  const configured = voicePipelineForStage(voicePipeline.stage);
  const isStaleConnectionDetail =
    voicePipeline.detail === "Connecting to the room." ||
    voicePipeline.detail === "Voice is disconnected." ||
    voicePipeline.detail === "Voice is disconnecting." ||
    voicePipeline.detail === "Reconnecting voice.";
  return {
    description:
      voicePipeline.stage === "idle" && isStaleConnectionDetail
        ? configured.description
        : voicePipeline.detail || configured.description,
    label: configured.label,
    stage: voicePipeline.stage,
  };
}

function voicePipelineForAgentState(
  rawState: string,
): Pick<VoicePipelineState, "detail" | "stage"> | null {
  const state = rawState.trim().toLowerCase();
  if (state === "listening") {
    return {
      detail: "The agent received the open-turn packet.",
      stage: "listening",
    };
  }
  if (state === "transcribing") {
    return {
      detail: "Audio sent. Waiting for transcription.",
      stage: "transcribing",
    };
  }
  if (state === "thinking" || state === "busy" || state === "running") {
    return {
      detail: "Transcript committed. Friday is working.",
      stage: "thinking",
    };
  }
  if (state === "speaking") {
    return {
      detail: "Friday is playing audio.",
      stage: "speaking",
    };
  }
  if (state === "idle") {
    return {
      detail: "Friday is ready.",
      stage: "idle",
    };
  }
  return null;
}

function voicePipelineForStage(stage: VoicePipelineStage) {
  switch (stage) {
    case "starting":
      return {
        description: "Opening the microphone.",
        label: "starting turn",
      };
    case "listening":
      return {
        description: "Microphone audio is live.",
        label: "listening",
      };
    case "sending":
      return {
        description: "Sending the end-of-turn packet.",
        label: "sending audio",
      };
    case "transcribing":
      return {
        description: "Audio sent. Waiting for transcription.",
        label: "transcribing",
      };
    case "thinking":
      return {
        description: "Transcript committed. Friday is working.",
        label: "thinking",
      };
    case "speaking":
      return {
        description: "Friday is playing audio.",
        label: "speaking",
      };
    case "complete":
      return {
        description: "Friday sent a response.",
        label: "response received",
      };
    case "error":
      return {
        description: "The last turn failed.",
        label: "needs attention",
      };
    case "idle":
    default:
      return {
        description: "No active turn.",
        label: "ready",
      };
  }
}

function getVoicePipelineStepState(
  stage: VoicePipelineStage,
  step: VoicePipelineStepId,
): VoicePipelineStepState {
  if (stage === "error") {
    return step === "process" ? "error" : "idle";
  }

  const activeStep = activeVoicePipelineStep(stage);
  if (activeStep === null) {
    return stage === "complete" ? "done" : "idle";
  }
  if (step === activeStep) {
    return stage === "complete" ? "done" : "active";
  }
  return voicePipelineStepIndex(step) < voicePipelineStepIndex(activeStep)
    ? "done"
    : "idle";
}

function activeVoicePipelineStep(
  stage: VoicePipelineStage,
): VoicePipelineStepId | null {
  if (stage === "starting" || stage === "listening") {
    return "capture";
  }
  if (stage === "sending") {
    return "send";
  }
  if (stage === "transcribing" || stage === "thinking") {
    return "process";
  }
  if (stage === "speaking" || stage === "complete") {
    return "reply";
  }
  return null;
}

function voicePipelineStepIndex(step: VoicePipelineStepId) {
  return VOICE_PIPELINE_STEPS.findIndex((item) => item.id === step);
}

function getRoomStatus({
  audioPlaybackReady,
  connectionState,
  isHolding,
  isSending,
  remoteParticipants,
}: {
  audioPlaybackReady: boolean;
  connectionState: ConnectionState;
  isHolding: boolean;
  isSending: boolean;
  remoteParticipants: RemoteParticipantSummary[];
}) {
  if (connectionState === ConnectionState.Disconnected) {
    return {
      description: "Connecting starts automatically when the room is ready.",
      label: "disconnected",
    };
  }

  if (
    connectionState === ConnectionState.Connecting ||
    connectionState === ConnectionState.Reconnecting
  ) {
    return {
      description: "Negotiating LiveKit transport.",
      label: "connecting",
    };
  }

  if (isHolding) {
    return {
      description: "Microphone is hot. Tap send to finish the turn.",
      label: "listening",
    };
  }

  if (isSending) {
    return {
      description: "Sending turn control to the Friday agent.",
      label: "sending",
    };
  }

  if (audioPlaybackReady) {
    return {
      description: "Friday audio playback is active.",
      label: "speaker active",
    };
  }

  if (remoteParticipants.length > 0) {
    return {
      description: "Agent participant is present.",
      label: "ready",
    };
  }

  return {
    description: "Connected and waiting for Friday-side audio.",
    label: "ready",
  };
}

function formatConversationRole(role: ConversationRole) {
  if (role === "user") {
    return "You";
  }
  if (role === "friday") {
    return "Friday";
  }
  return "System";
}

function formatProviderRole(role: string) {
  if (!role) {
    return "Unknown";
  }
  return role.charAt(0).toUpperCase() + role.slice(1);
}

function formatTranscriptTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return timeFormatter.format(date);
}

function stringValue(value: unknown) {
  return typeof value === "string" ? value.trim() : "";
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function hasProviderTranscriptContent(entry: TranscriptEntry) {
  if (entry.error || entry.text.trim()) {
    return true;
  }
  return visibleProviderParts(entry.parts ?? []).some((part) => {
    const type = stringValue(part.type);
    if (type === "text") {
      return Boolean(stringValue(part.text) || stringValue(part.content));
    }
    return Boolean(type);
  });
}

function visibleProviderParts(parts: Record<string, unknown>[]) {
  return parts.filter((part) => {
    const type = stringValue(part.type);
    if (type === "step-start" || type === "step-finish") {
      return false;
    }
    if (type === "text") {
      return Boolean(stringValue(part.text) || stringValue(part.content));
    }
    return true;
  });
}

function formatProviderPartSummary({
  partIndex,
  title,
  toolName,
  type,
}: {
  partIndex: number;
  title: string;
  toolName: string;
  type: string;
}) {
  if (title && toolName) {
    return `${type}: ${toolName} - ${title}`;
  }
  if (toolName) {
    return `${type}: ${toolName}`;
  }
  if (title) {
    return title;
  }
  return `${type} ${partIndex + 1}`;
}

function formatProviderValue(value: unknown) {
  if (value === undefined) {
    return "undefined";
  }
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function summarizeRemoteParticipants(
  participants: Iterable<RemoteParticipant>,
  activeSpeakers: Participant[],
): RemoteParticipantSummary[] {
  const speakingIds = new Set(
    activeSpeakers.map((speaker) => speaker.identity),
  );

  return Array.from(participants)
    .filter(isFridayAgentParticipant)
    .map((participant) => {
      const audioTrackCount = Array.from(
        participant.audioTrackPublications.values(),
      ).length;
      const subscribedAudioCount = Array.from(
        participant.audioTrackPublications.values(),
      ).filter((publication) => publication.isSubscribed).length;

      return {
        audioTrackCount,
        id: participant.sid ?? participant.identity,
        identity: participant.identity,
        isSpeaking: speakingIds.has(participant.identity),
        name: formatParticipantLabel(participant),
        subscribedAudioCount,
      };
    })
    .sort((left, right) => left.name.localeCompare(right.name));
}

async function waitForMicrophonePublication(
  participant: LocalParticipant,
  timeoutMs = 2000,
) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const hasMicrophonePublication = Array.from(
      participant.audioTrackPublications.values(),
    ).some((publication) => publication.source === Track.Source.Microphone);
    if (hasMicrophonePublication) {
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 50));
  }
}

function isFridayAgentParticipant(participant: RemoteParticipant) {
  return participant.isAgent || participant.kind === ParticipantKind.AGENT;
}

function formatParticipantLabel(participant: {
  identity: string;
  name?: string;
}) {
  return participant.name?.trim() || participant.identity;
}

function encodeTurnControlPayload(
  type: TurnControlType,
  options: { speakerEnabled?: boolean; text?: string } = {},
) {
  return JSON.stringify({
    type,
    ...(typeof options.speakerEnabled === "boolean"
      ? { speaker_enabled: options.speakerEnabled }
      : {}),
    ...(typeof options.text === "string" ? { text: options.text } : {}),
  });
}

function parseTurnControlRpcResult(response: string): TurnControlRpcResult {
  try {
    const parsed = JSON.parse(response) as { [key: string]: unknown };
    return {
      ok: parsed.ok === true,
      message: typeof parsed.message === "string" ? parsed.message : undefined,
      state: typeof parsed.state === "string" ? parsed.state : undefined,
      transcript:
        typeof parsed.transcript === "string" ? parsed.transcript : undefined,
      type: isTurnControlType(parsed.type) ? parsed.type : undefined,
    };
  } catch {
    return {
      ok: false,
      message: "Friday returned an invalid turn command response.",
    };
  }
}

function isTurnControlType(value: unknown): value is TurnControlType {
  return (
    value === "start_turn" ||
    value === "end_turn" ||
    value === "cancel_turn" ||
    value === "set_speaker" ||
    value === "submit_text"
  );
}

function parseAgentResponseMessage(payload: Uint8Array): AgentResponseMessage | null {
  const rawText = decodeUtf8(payload);
  if (!rawText) {
    return null;
  }

  try {
    const parsed = JSON.parse(rawText) as { [key: string]: unknown };
    if (typeof parsed.type !== "string") {
      return null;
    }

    return {
      event_id: typeof parsed.event_id === "number" ? parsed.event_id : undefined,
      message: typeof parsed.message === "string" ? parsed.message : undefined,
      name: typeof parsed.name === "string" ? parsed.name : undefined,
      state: typeof parsed.state === "string" ? parsed.state : undefined,
      text: typeof parsed.text === "string" ? parsed.text : undefined,
      type: parsed.type,
    };
  } catch {
    return null;
  }
}

function getAgentResponseText(parsed: AgentResponseMessage) {
  return [parsed.text, parsed.message, parsed.state, parsed.name]
    .find((value) => typeof value === "string" && value.trim().length > 0)
    ?.trim();
}

function decodeUtf8(payload: Uint8Array) {
  try {
    return textDecoder.decode(payload).trim();
  } catch {
    return "";
  }
}

function joinClassNames(...parts: Array<string | undefined>) {
  return parts.filter(Boolean).join(" ");
}

function isTextEntryTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) {
    return false;
  }

  return (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target.isContentEditable
  );
}
