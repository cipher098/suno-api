/** Shared pre-submission authentication. No generation submission or POST retries here. */
export class GenerationAuthError extends Error {
  constructor(public code: string) { super(code); }
}
export interface GenerationAuthOptions {
  check: () => Promise<unknown>;
  solve: () => Promise<string | null>;
  allowBrowser: boolean;
}
export async function prepareGenerationAuth(options: GenerationAuthOptions): Promise<string | null> {
  const required = await options.check();
  if (required === false) return null;
  if (required !== true) throw new GenerationAuthError('CAPTCHA_CHECK_FAILED');
  if (!options.allowBrowser) throw new GenerationAuthError('CAPTCHA_REQUIRED');
  try {
    const token = await options.solve();
    if (typeof token !== 'string' || !token.trim()) throw new GenerationAuthError('CAPTCHA_TOKEN_MISSING');
    return token;
  } catch (error) {
    if (error instanceof GenerationAuthError) throw error;
    // Browser/solver exceptions can contain headers, screenshots or credentials.
    throw new GenerationAuthError('CAPTCHA_BROWSER_FAILED');
  }
}

/** Serialize account browser challenges; never share a single-use token across callers. */
export class GenerationAuthGate {
  private tail: Promise<unknown> = Promise.resolve();
  prepare(options: GenerationAuthOptions): Promise<string | null> {
    const result = this.tail.then(() => prepareGenerationAuth(options));
    this.tail = result.then(() => undefined, () => undefined);
    return result;
  }
}
