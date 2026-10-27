import { GenerationAuthError } from './GenerationAuth';

/** Observe render parameters before the widget script loads. Secrets stay in-page. */
export async function installTurnstileCapture(page: any) {
  await page.addInitScript(() => {
    const w = window as any;
    let current: any;
    const wrap = (value: any) => {
      if (!value || typeof value.render !== 'function' || value.__sunoObserved) return value;
      const render = value.render;
      value.render = function(container: any, params: any) {
        if (params && typeof params.sitekey === 'string') {
          w.__sunoTurnstileCapture = {
            params: { sitekey: params.sitekey, action: params.action, data: params.cData,
              pagedata: params.chlPageData, pageurl: location.origin + location.pathname,
              useragent: navigator.userAgent },
            callback: params.callback
          };
          document.documentElement?.setAttribute('data-suno-turnstile-params', JSON.stringify(w.__sunoTurnstileCapture.params));
        }
        return render.call(this, container, params);
      };
      value.__sunoObserved = true;
      return value;
    };
    document.addEventListener('suno-turnstile-solved', (event: any) => {
      const capture = w.__sunoTurnstileCapture;
      const callback = typeof capture?.callback === 'string' ? w[capture.callback] : capture?.callback;
      if (typeof callback === 'function' && typeof event.detail === 'string') {
        callback(event.detail); document.documentElement?.setAttribute('data-suno-turnstile-delivered', '1');
      }
    });
    Object.defineProperty(w, 'turnstile', { configurable: true, get: () => current,
      set: value => { current = wrap(value); } });
  });
}

/** Exactly one solver submission for the observed widget; no retries or music POST. */
export async function solveObservedTurnstile(page: any, solve: (params: any) => Promise<any>, signal: AbortSignal) {
  // rebrowser evaluates in an isolated world. DOM attributes/events bridge to
  // the page's widget callback without assuming shared window globals.
  const params = await page.evaluate(() => {
    const raw = document.documentElement.getAttribute('data-suno-turnstile-params');
    return raw ? JSON.parse(raw) : null;
  });
  if (!params || typeof params.sitekey !== 'string' || !params.sitekey ||
      typeof params.pageurl !== 'string' || !/^https:\/\/suno\.com\/(?:create|studio)$/.test(params.pageurl)) {
    throw new GenerationAuthError('TURNSTILE_PARAMETERS_UNAVAILABLE');
  }
  if (signal.aborted) throw new GenerationAuthError('CAPTCHA_BROWSER_TIMEOUT');
  let answer;
  try { answer = await solve(params); }
  catch { throw new GenerationAuthError('TURNSTILE_SOLVER_FAILED'); }
  if (signal.aborted) throw new GenerationAuthError('CAPTCHA_BROWSER_TIMEOUT');
  if (typeof answer?.data !== 'string' || !answer.data.trim()) throw new GenerationAuthError('CAPTCHA_TOKEN_MISSING');
  const delivered = await page.evaluate((token: string) => {
    document.documentElement.removeAttribute('data-suno-turnstile-delivered');
    document.dispatchEvent(new CustomEvent('suno-turnstile-solved', { detail: token }));
    return document.documentElement.getAttribute('data-suno-turnstile-delivered') === '1';
  }, answer.data);
  if (!delivered) throw new GenerationAuthError('TURNSTILE_CALLBACK_UNAVAILABLE');
}
