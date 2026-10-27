import { GenerationAuthError } from './GenerationAuth';

// Capture and abort BEFORE the Create button is clicked. Browser UI must never
// send an incidental music-generation POST while gathering authentication.
export const generationRequestPattern = /^https:\/\/(?:studio-api\.prod\.suno\.com|suno\.com)\/(?:a\/)?api\/generate\/[^?#]*(?:\?.*)?$/;

function subject(jwt: string): string {
  try {
    const value = JSON.parse(Buffer.from(jwt.split('.')[1], 'base64url').toString()).sub;
    if (typeof value === 'string' && value) return value;
  } catch { /* Never expose token contents. */ }
  throw new GenerationAuthError('CAPTCHA_ACCOUNT_IDENTITY_INVALID');
}

export async function captureBrowserGenerationToken(options: {
  page: any;
  expectedUser: string;
  trigger: (signal: AbortSignal) => Promise<void>;
  close: () => Promise<unknown>;
  acceptBearer: (bearer: string) => void;
  timeoutMs?: number;
}): Promise<string> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let resolveToken!: (token: string) => void;
  let rejectToken!: (error: unknown) => void;
  const captured = new Promise<string>((resolve, reject) => { resolveToken = resolve; rejectToken = reject; });
  const handler = async (route: any) => {
    // Abort even malformed/wrong-account requests before inspecting their body.
    try {
      await route.abort();
      const request = route.request();
      if (!/\/api\/generate\/v2(?:-web)?\/?$/.test(new URL(request.url()).pathname)) throw new GenerationAuthError('CAPTCHA_GENERATION_ENDPOINT_UNSUPPORTED');
      if (request.method() !== 'POST') throw new GenerationAuthError('CAPTCHA_CAPTURE_INVALID');
      const bearer = request.headers().authorization?.match(/^Bearer (.+)$/)?.[1];
      if (!bearer || subject(bearer) !== options.expectedUser) throw new GenerationAuthError('CAPTCHA_ACCOUNT_MISMATCH');
      const token = request.postDataJSON()?.token;
      if (typeof token !== 'string' || !token.trim()) throw new GenerationAuthError('CAPTCHA_TOKEN_MISSING');
      options.acceptBearer(bearer);
      resolveToken(token);
    } catch (error) { rejectToken(error instanceof GenerationAuthError ? error : new GenerationAuthError('CAPTCHA_CAPTURE_FAILED')); }
  };
  try {
    await options.page.route(generationRequestPattern, handler);
    timer = setTimeout(() => rejectToken(new GenerationAuthError('CAPTCHA_BROWSER_TIMEOUT')), options.timeoutMs ?? 90000);
    // Observe trigger failures without creating an unhandled background rejection.
    void Promise.resolve().then(() => options.trigger(controller.signal)).catch(error => {
      if (!controller.signal.aborted) rejectToken(error instanceof GenerationAuthError ? error : new GenerationAuthError('CAPTCHA_BROWSER_FAILED'));
    });
    return await captured;
  } finally {
    if (timer) clearTimeout(timer);
    controller.abort();
    // Close first; do not remove the interception while a UI generation could be pending.
    await options.close().catch(() => undefined);
  }
}
