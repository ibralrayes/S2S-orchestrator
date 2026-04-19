import { NextResponse } from 'next/server';
import { getNusukToken, invalidateNusukToken } from '@/lib/nusukAuth';

export const revalidate = 0;
export const runtime = 'nodejs';

type ChatBody = {
  query?: string;
  session_id?: string;
  language?: string;
  tool?: string;
};

async function postChat(token: string, body: ChatBody) {
  const nusukUrl = process.env.NUSUK_URL;
  if (!nusukUrl) throw new Error('NUSUK_URL not configured');
  return fetch(`${nusukUrl.replace(/\/$/, '')}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
}

export async function POST(req: Request) {
  const started = Date.now();
  let payload: ChatBody;
  try {
    payload = (await req.json()) as ChatBody;
  } catch {
    return new NextResponse('Invalid JSON body', { status: 400 });
  }
  if (!payload.query || typeof payload.query !== 'string') {
    return new NextResponse('Missing "query"', { status: 400 });
  }

  const prefix = process.env.NUSUK_QUERY_PREFIX;
  const query = prefix ? `${prefix.trim()} ${payload.query}` : payload.query;

  const body: ChatBody = {
    query,
    session_id: payload.session_id,
    language: payload.language ?? 'ar',
    tool: payload.tool ?? 'Knowledge',
  };

  try {
    let token = await getNusukToken();
    let response = await postChat(token, body);
    if (response.status === 401) {
      invalidateNusukToken();
      token = await getNusukToken(true);
      response = await postChat(token, body);
    }
    const text = await response.text();
    console.info(
      `[ptt/chat] status=${response.status} query_len=${payload.query.length} duration_ms=${Date.now() - started}`
    );
    if (!response.ok) {
      return new NextResponse(text || `Nusuk error ${response.status}`, {
        status: response.status,
      });
    }
    return new NextResponse(text, {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error('[ptt/chat] failed', error);
    return new NextResponse(error instanceof Error ? error.message : 'Chat failed', {
      status: 502,
    });
  }
}
