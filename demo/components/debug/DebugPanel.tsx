'use client';

export type TraceMetric = {
  label: string;
  durationMs: number;
};

export type RequestTrace = {
  id: string;
  label: string;
  startedAt: number;
  endedAt?: number;
  durationMs?: number;
  payloadSummary?: string;
  resultSummary?: string;
  error?: string;
  stepTimings?: TraceMetric[];
};

type DebugPanelProps = {
  debugMode: boolean;
  traces: RequestTrace[];
  onToggle: () => void;
  onClear: () => void;
};

export function createTraceId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `trace-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function summarizeText(text: string, maxLength = 96): string {
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength - 1)}…`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatTimestamp(value?: number): string {
  if (!value) {
    return '—';
  }
  const date = new Date(value);
  return `${date.toLocaleTimeString([], { hour12: false })}.${String(date.getMilliseconds()).padStart(3, '0')}`;
}

function formatDuration(durationMs?: number): string {
  if (durationMs == null) {
    return '—';
  }
  if (durationMs >= 1000) {
    return `${(durationMs / 1000).toFixed(2)} s`;
  }
  return `${Math.round(durationMs)} ms`;
}

function toneClasses(trace: RequestTrace): string {
  if (trace.error) {
    return 'border-red-500/30 bg-red-500/10';
  }
  if (trace.endedAt == null) {
    return 'border-cyan-400/20 bg-cyan-400/10';
  }
  return 'border-white/10 bg-black/20';
}

export function DebugPanel({ debugMode, traces, onToggle, onClear }: DebugPanelProps) {
  return (
    <section className="font-mono">
      <div className="flex items-center justify-end gap-2">
        {debugMode ? (
          <button
            type="button"
            onClick={onClear}
            className="cursor-pointer rounded-full border border-white/15 bg-white/5 px-3 py-1 text-[11px] tracking-[0.18em] text-white/60 uppercase transition hover:bg-white/10 hover:text-white"
          >
            Clear
          </button>
        ) : null}
        <button
          type="button"
          onClick={onToggle}
          className="cursor-pointer rounded-full border border-white/15 bg-white/5 px-3 py-1 text-[11px] tracking-[0.18em] text-white/60 uppercase transition hover:bg-white/10 hover:text-white"
        >
          {debugMode ? 'Hide Debug' : 'Debug'}
        </button>
      </div>

      {debugMode ? (
        <div className="mt-4 rounded-[28px] border border-white/10 bg-white/5 p-4 shadow-2xl shadow-black/20 backdrop-blur">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <div className="text-[11px] tracking-[0.18em] text-white/50 uppercase">
                Debug Mode
              </div>
              <div className="mt-1 text-sm text-white/70">
                Request timings, payload summaries, and failure details.
              </div>
            </div>
            <div className="rounded-full bg-emerald-400/15 px-3 py-1 text-[11px] tracking-[0.18em] text-emerald-200 uppercase">
              {traces.length} traces
            </div>
          </div>

          {traces.length === 0 ? (
            <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-6 text-xs text-white/50">
              No request traces yet.
            </div>
          ) : (
            <div className="max-h-[420px] space-y-3 overflow-y-auto pr-1">
              {traces.map((trace) => (
                <article
                  key={trace.id}
                  className={`rounded-2xl border px-4 py-3 text-xs leading-6 ${toneClasses(trace)}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="font-semibold tracking-[0.16em] text-white uppercase">
                      {trace.label}
                    </div>
                    <div className="text-white/45">
                      {trace.error ? 'failed' : trace.endedAt == null ? 'running' : 'done'}
                    </div>
                  </div>

                  <div className="mt-2 text-white/65">
                    start: {formatTimestamp(trace.startedAt)}
                  </div>
                  <div className="text-white/65">end: {formatTimestamp(trace.endedAt)}</div>
                  <div className="text-white/65">duration: {formatDuration(trace.durationMs)}</div>

                  {trace.payloadSummary ? (
                    <div className="mt-2 text-white/75">payload: {trace.payloadSummary}</div>
                  ) : null}
                  {trace.resultSummary ? (
                    <div className="text-white/75">result: {trace.resultSummary}</div>
                  ) : null}
                  {trace.stepTimings?.length ? (
                    <div className="text-white/75">
                      steps:{' '}
                      {trace.stepTimings
                        .map((metric) => `${metric.label} ${formatDuration(metric.durationMs)}`)
                        .join(' · ')}
                    </div>
                  ) : null}
                  {trace.error ? (
                    <div className="mt-2 text-red-200">error: {trace.error}</div>
                  ) : null}
                </article>
              ))}
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}
