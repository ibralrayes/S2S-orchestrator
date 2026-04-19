import { NextResponse } from 'next/server';

export const revalidate = 0;
export const runtime = 'nodejs';

export async function GET() {
  return NextResponse.json({
    asrToken: process.env.ASR_TOKEN || null,
    browserAsrUrl: process.env.PTT_BROWSER_ASR_URL || null,
    browserTtsUrl: process.env.PTT_BROWSER_TTS_URL || null,
    directChatSupported: false,
  });
}
