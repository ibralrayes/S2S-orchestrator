import { NextResponse } from 'next/server';

export const revalidate = 0;
export const runtime = 'nodejs';

export async function POST(req: Request) {
  const asrUrl = process.env.ASR_URL;
  const asrToken = process.env.ASR_TOKEN;
  if (!asrUrl) {
    return new NextResponse('ASR_URL is not configured', { status: 500 });
  }

  const started = Date.now();
  const inbound = await req.formData();
  const file = inbound.get('file');
  if (!(file instanceof Blob)) {
    return new NextResponse('Missing "file" field', { status: 400 });
  }

  const forward = new FormData();
  const filename = (file as File).name || 'audio.webm';
  forward.append('file', file, filename);

  const target = `${asrUrl.replace(/\/$/, '')}/api/transcribe/`;
  const headers: Record<string, string> = {};
  if (asrToken) headers.Authorization = `Bearer ${asrToken}`;

  try {
    const response = await fetch(target, {
      method: 'POST',
      headers,
      body: forward,
    });
    const bodyText = await response.text();
    console.info(
      `[ptt/transcribe] status=${response.status} bytes=${file.size} duration_ms=${Date.now() - started}`
    );
    if (!response.ok) {
      return new NextResponse(bodyText || `ASR error ${response.status}`, {
        status: response.status,
      });
    }
    return new NextResponse(bodyText, {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error('[ptt/transcribe] failed', error);
    return new NextResponse(error instanceof Error ? error.message : 'ASR failed', {
      status: 502,
    });
  }
}
