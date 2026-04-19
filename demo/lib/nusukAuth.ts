type CachedToken = {
  token: string;
  expiresAt: number;
};

let cached: CachedToken | null = null;
let refreshPromise: Promise<CachedToken> | null = null;

function decodeJwtExpiry(token: string): number | null {
  const parts = token.split('.');
  if (parts.length < 2) return null;
  try {
    const padded = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const pad = padded.length % 4 === 0 ? '' : '='.repeat(4 - (padded.length % 4));
    const json = Buffer.from(padded + pad, 'base64').toString('utf-8');
    const payload = JSON.parse(json);
    if (typeof payload.exp === 'number') {
      return payload.exp * 1000;
    }
  } catch {
    return null;
  }
  return null;
}

async function fetchFreshToken(): Promise<CachedToken> {
  const baseUrl = process.env.NUSUK_URL;
  const clientId = process.env.NUSUK_CLIENT_ID;
  const clientSecret = process.env.NUSUK_CLIENT_SECRET;
  if (!baseUrl || !clientId || !clientSecret) {
    throw new Error('NUSUK_URL / NUSUK_CLIENT_ID / NUSUK_CLIENT_SECRET must be set');
  }
  const response = await fetch(`${baseUrl.replace(/\/$/, '')}/auth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
  });
  if (!response.ok) {
    throw new Error(`Nusuk auth failed: ${response.status} ${await response.text()}`);
  }
  const payload = (await response.json()) as { access_token?: string };
  const token = payload.access_token;
  if (!token) throw new Error('Nusuk auth response missing access_token');
  const expiresAt = decodeJwtExpiry(token) ?? Date.now() + 60 * 60 * 1000;
  return { token, expiresAt };
}

export async function getNusukToken(forceRefresh = false): Promise<string> {
  const now = Date.now();
  if (!forceRefresh && cached && now + 60_000 < cached.expiresAt) {
    return cached.token;
  }
  if (!refreshPromise) {
    refreshPromise = fetchFreshToken()
      .then((value) => {
        cached = value;
        return value;
      })
      .finally(() => {
        refreshPromise = null;
      });
  }
  const fresh = await refreshPromise;
  return fresh.token;
}

export function invalidateNusukToken(): void {
  cached = null;
}
