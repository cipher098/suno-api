import { NextRequest, NextResponse } from 'next/server';

// Opt-in account-server mode. The launcher binds to loopback and requires a
// separate random local token. A request cannot switch this process's Suno account.
export function middleware(req: NextRequest) {
  if (process.env.SUNO_API_ACCOUNT_MODE !== '1') return NextResponse.next();
  const token = process.env.SUNO_API_LOCAL_TOKEN;
  if (!token || token.length < 32 || req.headers.get('authorization') !== `Bearer ${token}` ||
      req.headers.has('cookie') || req.headers.has('origin')) {
    return NextResponse.json({ error: 'LOCAL_ACCOUNT_AUTH_REQUIRED' }, { status: 401 });
  }
  return NextResponse.next();
}
export const config = { matcher: ['/api/:path*', '/v1/:path*'] };
