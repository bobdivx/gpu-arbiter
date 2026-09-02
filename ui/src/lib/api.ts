export type Slot = 'llm' | 'ace' | 'wan' | 'steam' | 'idle' | string;

export type GpuStatus = {
  ok: boolean;
  slot: Slot;
  owner: string | null;
  acquired_at: number | null;
  last_touch: number | null;
  switching: boolean;
  last_error: string | null;
  queue: Array<{ slot: string; owner: string; waited_s: number }>;
  vram_used_mib: number | null;
  procs: {
    llama: boolean;
    ace: boolean;
    litellm: boolean;
    steam_client: boolean;
    steam_game: boolean;
  };
  steam: {
    priority_enabled: boolean;
    active: boolean;
    detail: string | null;
  };
  idle_release_s: number;
  hint?: string;
  error?: string;
  message?: string;
};

async function parseJson(res: Response): Promise<GpuStatus> {
  const data = (await res.json()) as GpuStatus;
  if (!res.ok && !data.error) {
    data.error = `HTTP ${res.status}`;
  }
  return data;
}

export async function fetchStatus(): Promise<GpuStatus> {
  const res = await fetch('/status', { cache: 'no-store' });
  return parseJson(res);
}

export async function acquireSlot(
  slot: 'llm' | 'ace' | 'wan',
  start = true,
): Promise<GpuStatus> {
  const res = await fetch('/acquire', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      slot,
      owner: 'web-ui',
      timeout_s: 120,
      start,
    }),
  });
  return parseJson(res);
}

export async function releaseSlot(): Promise<GpuStatus> {
  const res = await fetch('/release', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ owner: 'web-ui' }),
  });
  return parseJson(res);
}
