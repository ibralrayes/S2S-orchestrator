import { NextResponse } from 'next/server';

export const revalidate = 0;
export const runtime = 'nodejs';

function stripMarkdown(text: string): string {
  return text
    .replace(/\*+([^*\n]+)\*+/g, '$1')
    .replace(/^\s*>+\s*/gm, '')
    .replace(/\[\d+\]/g, '')
    .replace(/\n{2,}/g, ' ')
    .trim();
}

export async function POST(req: Request) {
  const ttsUrl = process.env.TTS_URL;
  if (!ttsUrl) {
    return new NextResponse('TTS_URL is not configured', { status: 500 });
  }

  const started = Date.now();
  let body: { text?: string };
  try {
    body = (await req.json()) as { text?: string };
  } catch {
    return new NextResponse('Invalid JSON body', { status: 400 });
  }
  if (!body.text || typeof body.text !== 'string') {
    return new NextResponse('Missing "text"', { status: 400 });
  }

  const text = stripMarkdown(body.text);

  try {
    const response = await fetch(ttsUrl.replace(/\/$/, ''), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      const message = await response.text();
      console.error(`[ptt/tts] status=${response.status} body=${message.slice(0, 200)}`);
      return new NextResponse(message || `TTS error ${response.status}`, {
        status: response.status,
      });
    }
    const audio = await response.arrayBuffer();
    console.info(
      `[ptt/tts] status=${response.status} text_len=${body.text.length} audio_bytes=${audio.byteLength} duration_ms=${Date.now() - started}`
    );
    return new NextResponse(audio, {
      status: 200,
      headers: {
        'Content-Type': response.headers.get('content-type') ?? 'audio/wav',
        'Cache-Control': 'no-store',
      },
    });
  } catch (error) {
    console.error('[ptt/tts] failed', error);
    return new NextResponse(error instanceof Error ? error.message : 'TTS failed', {
      status: 502,
    });
  }
}
