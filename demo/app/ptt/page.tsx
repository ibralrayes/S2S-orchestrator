'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  DebugPanel,
  type RequestTrace,
  createTraceId,
  formatBytes,
  summarizeText,
} from '@/components/debug/DebugPanel';

type Stage = 'idle' | 'recording' | 'transcribing' | 'thinking' | 'speaking' | 'error';

type Turn = {
  id: string;
  user: string;
  assistant: string;
};

type PttDirectConfig = {
  asrToken: string | null;
  browserAsrUrl: string | null;
  browserTtsUrl: string | null;
  directChatSupported: boolean;
};

type RequestPath = 'direct' | 'proxy' | 'proxy-fallback';

function generateSessionId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `ptt-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function stageLabel(stage: Stage): string {
  switch (stage) {
    case 'recording':
      return 'Recording…';
    case 'transcribing':
      return 'Transcribing…';
    case 'thinking':
      return 'Thinking…';
    case 'speaking':
      return 'Speaking…';
    case 'error':
      return 'Error';
    default:
      return 'Idle';
  }
}

function stageTone(stage: Stage): string {
  switch (stage) {
    case 'recording':
      return 'bg-rose-400/15 text-rose-200';
    case 'transcribing':
      return 'bg-cyan-400/15 text-cyan-200';
    case 'thinking':
      return 'bg-amber-400/15 text-amber-200';
    case 'speaking':
      return 'bg-emerald-400/15 text-emerald-200';
    case 'error':
      return 'bg-red-500/20 text-red-200';
    default:
      return 'bg-white/10 text-white/60';
  }
}

function resolveBrowserUrl(explicitUrl: string | null, port: number, path: string): string {
  if (explicitUrl) {
    return explicitUrl;
  }
  const url = new URL(window.location.origin);
  url.port = String(port);
  url.pathname = path;
  url.search = '';
  return url.toString();
}

function requestPathLabel(path: RequestPath): string {
  switch (path) {
    case 'direct':
      return 'direct';
    case 'proxy-fallback':
      return 'proxy fallback';
    default:
      return 'proxy';
  }
}

function isFetchFailure(error: unknown): boolean {
  return error instanceof TypeError;
}

export default function PushToTalkPage() {
  const [stage, setStage] = useState<Stage>('idle');
  const [debugMode, setDebugMode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [directConfig, setDirectConfig] = useState<PttDirectConfig | null>(null);
  const [traces, setTraces] = useState<RequestTrace[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sessionId] = useState<string>(() => generateSessionId());

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const mimeTypeRef = useRef<string>('audio/webm');

  useEffect(() => {
    let cancelled = false;

    async function loadConfig() {
      try {
        const response = await fetch('/api/ptt/config', { cache: 'no-store' });
        if (!response.ok) {
          throw new Error(`PTT config failed (${response.status})`);
        }
        const payload = (await response.json()) as PttDirectConfig;
        if (!cancelled) {
          setDirectConfig(payload);
        }
      } catch (error) {
        console.error('[ptt/config] failed', error);
        if (!cancelled) {
          setDirectConfig({
            asrToken: null,
            browserAsrUrl: null,
            browserTtsUrl: null,
            directChatSupported: false,
          });
        }
      }
    }

    void loadConfig();

    return () => {
      cancelled = true;
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current.src = '';
      }
    };
  }, []);

  const appendTrace = useCallback((trace: RequestTrace) => {
    setTraces((prev) => [trace, ...prev]);
  }, []);

  const updateTrace = useCallback((traceId: string, patch: Partial<RequestTrace>) => {
    setTraces((prev) =>
      prev.map((trace) => (trace.id === traceId ? { ...trace, ...patch } : trace))
    );
  }, []);

  const clearTraces = useCallback(() => {
    setTraces([]);
  }, []);

  const fetchTranscribe = useCallback(
    async (body: FormData): Promise<{ response: Response; path: RequestPath }> => {
      if (directConfig) {
        try {
          const directHeaders: Record<string, string> = {};
          if (directConfig.asrToken) {
            directHeaders.Authorization = `Bearer ${directConfig.asrToken}`;
          }
          const response = await fetch(
            resolveBrowserUrl(directConfig.browserAsrUrl, 8102, '/api/transcribe/'),
            {
              method: 'POST',
              headers: directHeaders,
              body,
            }
          );
          return { response, path: 'direct' };
        } catch (error) {
          if (!isFetchFailure(error)) {
            throw error;
          }
          const response = await fetch('/api/ptt/transcribe', {
            method: 'POST',
            body,
          });
          return { response, path: 'proxy-fallback' };
        }
      }

      const response = await fetch('/api/ptt/transcribe', {
        method: 'POST',
        body,
      });
      return { response, path: 'proxy' };
    },
    [directConfig]
  );

  const fetchTts = useCallback(
    async (text: string): Promise<{ response: Response; path: RequestPath }> => {
      if (directConfig) {
        try {
          const response = await fetch(resolveBrowserUrl(directConfig.browserTtsUrl, 8000, '/'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
          });
          return { response, path: 'direct' };
        } catch (error) {
          if (!isFetchFailure(error)) {
            throw error;
          }
          const response = await fetch('/api/ptt/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
          });
          return { response, path: 'proxy-fallback' };
        }
      }

      const response = await fetch('/api/ptt/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      return { response, path: 'proxy' };
    },
    [directConfig]
  );

  const runPipeline = useCallback(
    async (blob: Blob) => {
      const turnId = generateSessionId();
      setError(null);

      setStage('transcribing');
      const transcribeForm = new FormData();
      transcribeForm.append('file', blob, 'utterance.webm');
      const transcribeTraceId = createTraceId();
      const transcribeStartedAt = Date.now();
      appendTrace({
        id: transcribeTraceId,
        label: 'Transcribe',
        startedAt: transcribeStartedAt,
        payloadSummary: `audio ${formatBytes(blob.size)} · ${blob.type || 'unknown format'} · direct preferred`,
      });

      let transcribeJson: {
        transcription_text?: string;
        text?: string;
        transcript?: string;
        transcription?: string;
        processing_time_seconds?: number;
      };

      try {
        const { response: transcribeResp, path } = await fetchTranscribe(transcribeForm);
        if (!transcribeResp.ok) {
          throw new Error(
            `Transcribe failed (${transcribeResp.status}): ${await transcribeResp.text()}`
          );
        }
        transcribeJson = (await transcribeResp.json()) as {
          transcription_text?: string;
          text?: string;
          transcript?: string;
          transcription?: string;
          processing_time_seconds?: number;
        };
        const endedAt = Date.now();
        const transcriptText = (
          transcribeJson.transcription_text ||
          transcribeJson.text ||
          transcribeJson.transcript ||
          transcribeJson.transcription ||
          'empty transcript'
        ).trim();
        updateTrace(transcribeTraceId, {
          endedAt,
          durationMs: endedAt - transcribeStartedAt,
          payloadSummary: `audio ${formatBytes(blob.size)} · ${blob.type || 'unknown format'} · ${requestPathLabel(path)}`,
          resultSummary: summarizeText(transcriptText),
          stepTimings:
            typeof transcribeJson.processing_time_seconds === 'number'
              ? [
                  {
                    label: 'backend',
                    durationMs: Math.round(transcribeJson.processing_time_seconds * 1000),
                  },
                ]
              : undefined,
        });
      } catch (error) {
        const endedAt = Date.now();
        updateTrace(transcribeTraceId, {
          endedAt,
          durationMs: endedAt - transcribeStartedAt,
          error: error instanceof Error ? error.message : 'Transcribe request failed',
        });
        throw error;
      }

      const transcript = (
        transcribeJson.transcription_text ||
        transcribeJson.text ||
        transcribeJson.transcript ||
        transcribeJson.transcription ||
        ''
      ).trim();
      if (!transcript) {
        throw new Error('Empty transcript — try speaking for a bit longer.');
      }

      setTurns((prev) => [...prev, { id: turnId, user: transcript, assistant: '' }]);

      setStage('thinking');
      const chatTraceId = createTraceId();
      const chatStartedAt = Date.now();
      appendTrace({
        id: chatTraceId,
        label: 'Chat',
        startedAt: chatStartedAt,
        payloadSummary: `query ${transcript.length} chars · session ${sessionId.slice(0, 8)} · proxy`,
      });

      let chatJson: { response?: string };
      try {
        const chatResp = await fetch('/api/ptt/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query: transcript, session_id: sessionId }),
        });
        if (!chatResp.ok) {
          throw new Error(`Chat failed (${chatResp.status}): ${await chatResp.text()}`);
        }
        chatJson = (await chatResp.json()) as { response?: string };
        const endedAt = Date.now();
        updateTrace(chatTraceId, {
          endedAt,
          durationMs: endedAt - chatStartedAt,
          resultSummary: summarizeText((chatJson.response || '').trim() || 'empty reply'),
        });
      } catch (error) {
        const endedAt = Date.now();
        updateTrace(chatTraceId, {
          endedAt,
          durationMs: endedAt - chatStartedAt,
          error: error instanceof Error ? error.message : 'Chat request failed',
        });
        throw error;
      }

      const reply = (chatJson.response || '').trim();
      if (!reply) {
        throw new Error('Empty reply from chat');
      }
      setTurns((prev) =>
        prev.map((turn) => (turn.id === turnId ? { ...turn, assistant: reply } : turn))
      );

      setStage('speaking');
      const ttsTraceId = createTraceId();
      const ttsStartedAt = Date.now();
      appendTrace({
        id: ttsTraceId,
        label: 'Speak',
        startedAt: ttsStartedAt,
        payloadSummary: `text ${reply.length} chars · direct preferred`,
      });

      let audioBlob: Blob;
      try {
        const { response: ttsResp, path } = await fetchTts(reply);
        if (!ttsResp.ok) {
          throw new Error(`TTS failed (${ttsResp.status}): ${await ttsResp.text()}`);
        }
        audioBlob = await ttsResp.blob();
        const endedAt = Date.now();
        const processingHeader = ttsResp.headers.get('x-processing-time');
        const processingMs =
          processingHeader && !Number.isNaN(Number(processingHeader))
            ? Math.round(Number(processingHeader) * 1000)
            : undefined;
        updateTrace(ttsTraceId, {
          endedAt,
          durationMs: endedAt - ttsStartedAt,
          payloadSummary: `text ${reply.length} chars · ${requestPathLabel(path)}`,
          resultSummary: `${formatBytes(audioBlob.size)} · ${audioBlob.type || 'audio/wav'}`,
          stepTimings:
            processingMs != null ? [{ label: 'backend', durationMs: processingMs }] : undefined,
        });
      } catch (error) {
        const endedAt = Date.now();
        updateTrace(ttsTraceId, {
          endedAt,
          durationMs: endedAt - ttsStartedAt,
          error: error instanceof Error ? error.message : 'TTS request failed',
        });
        throw error;
      }

      const url = URL.createObjectURL(audioBlob);
      const audio = new Audio(url);
      audioRef.current = audio;
      await audio.play().catch(() => undefined);
      audio.onended = () => {
        URL.revokeObjectURL(url);
        setStage('idle');
      };
    },
    [appendTrace, fetchTranscribe, fetchTts, sessionId, updateTrace]
  );

  const startRecording = useCallback(async () => {
    if (stage !== 'idle') return;
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const preferred = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
      const mimeType =
        preferred.find((candidate) => MediaRecorder.isTypeSupported(candidate)) ?? '';
      mimeTypeRef.current = mimeType || 'audio/webm';
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      chunksRef.current = [];
      recorder.ondataavailable = (ev) => {
        if (ev.data.size > 0) chunksRef.current.push(ev.data);
      };
      recorder.onstop = async () => {
        stream.getTracks().forEach((track) => track.stop());
        const blob = new Blob(chunksRef.current, {
          type: mimeTypeRef.current,
        });
        if (blob.size === 0) {
          setStage('idle');
          return;
        }
        try {
          await runPipeline(blob);
        } catch (err) {
          console.error(err);
          setError(err instanceof Error ? err.message : 'Pipeline failed');
          setStage('error');
        }
      };
      mediaRecorderRef.current = recorder;
      recorder.start();
      setStage('recording');
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'Microphone access failed');
      setStage('error');
    }
  }, [runPipeline, stage]);

  const stopRecording = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== 'inactive') {
      recorder.stop();
    }
  }, []);

  const buttonDisabled = stage !== 'idle' && stage !== 'recording' && stage !== 'error';

  return (
    <main className="min-h-screen bg-neutral-950 px-6 py-20 text-white">
      <div className="mx-auto flex max-w-3xl flex-col gap-8">
        <DebugPanel
          debugMode={debugMode}
          traces={traces}
          onToggle={() => setDebugMode((prev) => !prev)}
          onClear={clearTraces}
        />

        <header>
          <div className="mb-3 inline-flex items-center rounded-full border border-amber-400/20 bg-amber-400/10 px-3 py-1 text-[11px] font-semibold tracking-[0.2em] text-amber-200 uppercase">
            Push-to-talk Demo
          </div>
          <h1 className="text-3xl font-semibold tracking-tight">Modular ASR → Chat → TTS</h1>
          <p className="mt-3 text-sm leading-7 text-white/70">
            اضغط مع الاستمرار للتسجيل، ثم حرر الزر لإرسال طلبك. نفس خدمات الإنتاج: ASR محلي، محادثة
            Nusuk، وصوت TTS.
          </p>
        </header>

        <section className="rounded-[32px] border border-white/10 bg-white/5 p-10 text-center shadow-2xl shadow-black/30 backdrop-blur">
          <div
            className={`mx-auto mb-6 inline-flex rounded-full px-3 py-1 text-[11px] font-semibold tracking-[0.18em] uppercase ${stageTone(stage)}`}
          >
            {stageLabel(stage)}
          </div>

          <button
            type="button"
            disabled={buttonDisabled}
            onPointerDown={(ev) => {
              ev.preventDefault();
              void startRecording();
            }}
            onPointerUp={(ev) => {
              ev.preventDefault();
              stopRecording();
            }}
            onPointerLeave={() => {
              if (stage === 'recording') stopRecording();
            }}
            onPointerCancel={() => {
              if (stage === 'recording') stopRecording();
            }}
            className={`mx-auto flex h-40 w-40 cursor-pointer items-center justify-center rounded-full text-sm font-bold tracking-[0.2em] uppercase transition select-none ${
              stage === 'recording'
                ? 'scale-105 bg-rose-500 text-white shadow-2xl shadow-rose-500/40'
                : 'bg-cyan-400 text-black hover:bg-cyan-300'
            } ${buttonDisabled ? 'cursor-not-allowed opacity-50' : ''}`}
          >
            {stage === 'recording' ? 'Release' : 'Hold to talk'}
          </button>

          <div className="mt-6 text-xs tracking-[0.18em] text-white/40 uppercase">
            Session: {sessionId.slice(0, 8)}
          </div>

          {error ? (
            <div className="mt-6 rounded-2xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
              {error}
            </div>
          ) : null}
        </section>

        <section className="rounded-[32px] border border-white/10 bg-white/5 p-6 shadow-2xl shadow-black/30 backdrop-blur">
          <div className="mb-4 text-[11px] tracking-[0.18em] text-white/50 uppercase">
            Conversation
          </div>
          {turns.length === 0 ? (
            <div className="rounded-2xl border border-white/10 bg-black/20 p-6 text-sm text-white/60">
              لا توجد محادثات بعد. اضغط زر الميكروفون وتحدث بالعربية.
            </div>
          ) : (
            <div className="space-y-3">
              {turns.map((turn) => (
                <div key={turn.id} className="space-y-2">
                  <div className="rounded-2xl border border-cyan-500/20 bg-cyan-500/10 px-4 py-3 text-sm leading-6 text-cyan-50">
                    <div className="mb-1 text-[11px] font-semibold tracking-[0.18em] text-cyan-200/70 uppercase">
                      User
                    </div>
                    {turn.user}
                  </div>
                  {turn.assistant ? (
                    <div className="rounded-2xl border border-white/10 bg-white/10 px-4 py-3 text-sm leading-6 text-white">
                      <div className="mb-1 text-[11px] font-semibold tracking-[0.18em] text-white/50 uppercase">
                        Assistant
                      </div>
                      {turn.assistant}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
