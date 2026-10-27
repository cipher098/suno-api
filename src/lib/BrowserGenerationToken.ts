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
  acceptProvider?: (provider: number) => void;
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
      const payload = request.postDataJSON();
      const token = payload?.token;
      if (payload?.token_provider !== undefined) {
        if (!Number.isInteger(payload.token_provider) || payload.token_provider < 0 || payload.token_provider > 10) throw new GenerationAuthError('CAPTCHA_TOKEN_PROVIDER_UNSUPPORTED');
        options.acceptProvider?.(payload.token_provider);
      }
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

/** Prefer stable roles/current visible inputs; retain the older custom textarea. */
export async function selectGenerationControls(page: any) {
  await page.locator('textarea:visible').first().waitFor({ state: 'visible', timeout: 15000 });
  const legacy = page.locator('.custom-textarea:visible');
  const textarea = await legacy.count() ? legacy.first() : page.locator('textarea:visible').first();
  const button = page.getByRole('button', { name: /^(Create song|Create)$/, exact: true }).first();
  await button.waitFor({ state: 'visible', timeout: 15000 });
  return { textarea, button };
}

/** Auth probes use Simple mode so a remembered Advanced/lyricist form cannot
 * redirect typing into a different input. Dismiss the observed onboarding tip. */
export async function prepareGenerationPage(page: any) {
  const dismissTip = async () => {
    const button = page.getByRole('button', { name: 'Got it', exact: true }).first();
    if (await button.isVisible().catch(() => false)) await button.click();
  };
  await dismissTip();
  const tab = page.getByRole('tab', { name: 'Simple', exact: true }).first();
  if (await tab.count()) {
    await tab.click();
    await page.waitForFunction(() => Array.from(document.querySelectorAll('[role=tab]')).some(
      el => el.textContent?.trim() === 'Simple' && el.getAttribute('aria-selected') === 'true'), undefined, { timeout: 15000 });
  } else {
    const legacy = page.getByRole('button', { name: 'Simple', exact: true }).first();
    if (await legacy.count()) await legacy.click();
  }
  await dismissTip();
}
