import { useEffect, useState } from 'preact/hooks';
import {
  acquireSlot,
  fetchStatus,
  releaseSlot,
  type GpuStatus,
} from '../lib/api';

const SLOT_LABEL: Record<string, string> = {
  llm: 'LLM',
  ace: 'ACE',
  wan: 'Wan',
  steam: 'Steam',
  idle: 'Libre',
};

function formatError(status: GpuStatus | null, actionError: string | null): string | null {
  if (actionError) return actionError;
  if (!status) return null;
  if (status.error === 'steam_priority') {
    return status.message || 'Jeu Steam actif — GPU réservé.';
  }
  if (status.error) return status.message || status.error;
  if (status.last_error) return String(status.last_error);
  return null;
}

export function Dashboard() {
  const [status, setStatus] = useState<GpuStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [online, setOnline] = useState(true);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const next = await fetchStatus();
        if (!cancelled) {
          setStatus(next);
          setOnline(true);
        }
      } catch {
        if (!cancelled) setOnline(false);
      }
    };

    void poll();
    const id = window.setInterval(poll, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const run = async (fn: () => Promise<GpuStatus>) => {
    setBusy(true);
    setActionError(null);
    try {
      const next = await fn();
      setStatus(next);
      if (!next.ok) {
        setActionError(
          next.error === 'steam_priority'
            ? next.message || 'Priorité Steam'
            : next.message || next.error || 'Échec',
        );
      }
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Réseau indisponible');
      setOnline(false);
    } finally {
      setBusy(false);
    }
  };

  const slot = status?.slot ?? '…';
  const vram = status?.vram_used_mib;
  const steamOn = Boolean(status?.steam?.active);
  const err = formatError(status, actionError);

  return (
    <div class="dash">
      <header class="hero">
        <p class="brand">GPU Arbiter</p>
        <h1 class="slot-line">
          <span class={`slot-pill slot-${slot}`}>{SLOT_LABEL[slot] ?? slot}</span>
          <span class="vram">
            {vram != null ? `${Math.round(vram)} MiB` : '— MiB'}
          </span>
        </h1>
        <p class="lede">
          Un slot GPU à la fois — LLM, ACE ou Wan. Les jeux Steam passent avant.
        </p>
      </header>

      <div class="steam-row" data-active={steamOn ? '1' : '0'}>
        <span class="steam-dot" aria-hidden="true" />
        <span>
          {steamOn
            ? `Steam prioritaire${status?.steam?.detail ? ` — ${status.steam.detail}` : ''}`
            : status?.steam?.priority_enabled
              ? 'Steam inactif'
              : 'Priorité Steam désactivée'}
        </span>
      </div>

      <div class="actions" role="group" aria-label="Changer de slot">
        <button
          type="button"
          class="btn"
          disabled={busy || steamOn}
          onClick={() => void run(() => acquireSlot('llm'))}
        >
          LLM
        </button>
        <button
          type="button"
          class="btn"
          disabled={busy || steamOn}
          onClick={() => void run(() => acquireSlot('ace'))}
        >
          ACE
        </button>
        <button
          type="button"
          class="btn"
          disabled={busy || steamOn}
          onClick={() => void run(() => acquireSlot('wan'))}
        >
          Wan
        </button>
        <button
          type="button"
          class="btn btn-ghost"
          disabled={busy || steamOn}
          onClick={() => void run(() => releaseSlot())}
        >
          Release
        </button>
      </div>

      {err ? <p class="err" role="alert">{err}</p> : null}

      <ul class="procs">
        <li data-on={status?.procs?.llama ? '1' : '0'}>llama-server</li>
        <li data-on={status?.procs?.ace ? '1' : '0'}>ACE Studio</li>
        <li data-on={status?.procs?.litellm ? '1' : '0'}>LiteLLM</li>
        <li data-on={online ? '1' : '0'}>{online ? 'API ok' : 'API hors ligne'}</li>
      </ul>

      {status?.owner ? (
        <p class="meta">
          Owner <code>{status.owner}</code>
          {status.switching ? ' · switching…' : ''}
        </p>
      ) : null}
    </div>
  );
}
