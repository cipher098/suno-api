# Experimental Studio API

These methods adapt an observed Suno Studio flow into the existing authenticated
`SunoApi` instance. Existing generation, clip, quota and aligned-lyrics routes remain
unchanged. This is an unofficial, version-sensitive interface, not an official
Suno API or a guarantee of account entitlement, model availability or downloads.

Use an account with Studio access and an **existing project owned by that account**.
The methods do not create projects or save/overwrite timelines. By default, a
CAPTCHA requirement stops generation with `CAPTCHA_REQUIRED`. An explicit
`SUNO_STUDIO_BROWSER_FALLBACK=true` opts into the repository browser/solver helper
before the Studio generation POST; see the compatibility notes below.

## Endpoints

All accept the existing `SUNO_COOKIE` configuration or request Cookie header.
Protect the server behind authentication; the project does not add an API access
control layer. Never expose a credential-backed instance publicly without one.

| Method | Route | Input / result |
| --- | --- | --- |
| GET | `/api/studio/project?id=PROJECT_UUID` | Owned project metadata; foreign ownership is rejected. |
| POST | `/api/studio/generate` | `project_id`, `prompt` (lyrics), `tags` (style), optional `title`, `model`. Returns provider generation response with `clips[].id`. |
| GET | `/api/studio/status?ids=UUID,UUID` | Provider feed response including clip statuses, metadata and moderation errors. |
| POST | `/api/studio/export` | `project_id`, `clip_id`, `duration_seconds`, optional `state`, `title`, `lyrics`, `tags`. Returns rendered recording `id`. |
| GET | `/api/studio/download?id=RENDER_UUID` | Provider download-preparation status and eventual signed `download_url`, M4A only. |

`generate` defaults to the observed provider identifier `chirp-hawk`; this is not
an assertion that a marketing model name or preview is available on every account.
Style length is limited to 1,000 characters (the tested operational bound).

Each call is one stage. Generation and rendering are asynchronous. **Persist every
returned source and render ID before polling.** Poll status until the selected
source/render is `complete`; `streaming` is not ready for export. On provider `error`
stop and inspect the error; never automatically regenerate after moderation failure.

A request timeout for generation/render reports `submission_unknown: true`. This
means the provider may have accepted the request. Reconcile the account's existing
jobs before another POST. This adapter does not supply durable job storage or
cross-request idempotency; a repeated POST is a new request and may consume credits.
The transaction UUID is tracing metadata, not an idempotency guarantee.

## Example sequence

```sh
curl -sS http://localhost:3000/api/studio/generate \
  -H 'Content-Type: application/json' \
  --data '{"project_id":"YOUR_OWNED_PROJECT_UUID","prompt":"Your original lyrics","tags":"Bright rhythmic acoustic pop","title":"Example"}'

# Persist the chosen clips[].id and poll until complete.
curl -sS 'http://localhost:3000/api/studio/status?ids=SOURCE_UUID'

# Prepare export-input.json as described below, then submit exactly once.
curl -sS http://localhost:3000/api/studio/export \
  -H 'Content-Type: application/json' --data-binary @export-input.json

# Persist the returned render id. Poll until complete, then request its download.
curl -sS 'http://localhost:3000/api/studio/status?ids=RENDER_UUID'
curl -sS 'http://localhost:3000/api/studio/download?id=RENDER_UUID'

# Use the EXPORTED recording ID for its timing, not the source take's ID.
curl -sS 'http://localhost:3000/api/get_aligned_lyrics?song_id=RENDER_UUID'
```

The export input's `state` is a current Studio timeline state containing an audio
track with at least one clip; it supplies the version-sensitive track/clip schema.
Obtain it from your own project/state or a locally inspected Studio render request.
Do not commit HARs: they contain cookies, bearer tokens and signed URLs. Project
response shapes may change; project lookup is not a guarantee that the response
itself contains a render-ready state. A clean account-independent template is bundled as the default when `state` is omitted. Custom `state` remains supported.

```json
{
  "project_id": "YOUR_OWNED_PROJECT_UUID",
  "clip_id": "YOUR_COMPLETED_SOURCE_UUID",
  "duration_seconds": 204,
  "state": { "tracks": ["REPLACE_WITH_CURRENT_AUDIO_TRACK_OBJECT"] },
  "title": "Example",
  "lyrics": "Your original lyrics",
  "tags": "Bright rhythmic acoustic pop"
}
```

The placeholder `state` above is intentionally not a valid schema template. Supply
your real track object. `duration_seconds` must match the completed source metadata
within 250 ms and be between zero and eight minutes. Eight minutes is this adapter's
validated operational bound, not a claim about Suno's maximum.

Export clones the template, selects one audio track/clip, replaces its source,
removes other tracks/take lanes, disables warp/loop/streaming and uses a complete
zero-offset range at a fixed two beats per second. It does not save the project.
This prevents accidental truncation/time-stretching seen with reusing a selected
UI range. Other supplied track settings/effects are retained; use a clean template
if you want an unprocessed export. Decode/listen to downloaded media to verify
non-silence, duration and content; an HTTP success does not establish audio quality.

`download` returns `processing` while preparing. Poll the same ID later; do not
render again. When ready, download the signed URL using a **separate unauthenticated
HTTP client**. Do not send Suno cookies or Authorization to the file host. The API
validates the observed export host/path and does not proxy bytes or follow arbitrary
URLs. Keep signed URLs private; they expire. No limit-bypass claim is made here.

Word timings come from the existing `/api/get_aligned_lyrics` method. They may contain
zero-duration words, repetitions or errors; preserve the raw response. Timed lyrics
do not prove that every supplied word was sung correctly.

## Validation and scope

`npm run test:studio` runs offline transport-mocked tests without credentials,
real browser, provider requests or credits. They cover payloads, ownership, CAPTCHA,
no implicit retries, full-length non-warping state, incomplete/mismatched source,
polling, pending downloads and signed-destination validation.

The underlying Python prototype previously generated, rendered and downloaded a
complete track and retrieved its exported alignment on an authenticated account.
This TypeScript integration is covered by offline tests and read-only checks;
no additional paid generation/render was made for the contribution. The default clean template is based on the Python prototype; end-to-end compatibility across accounts,
Suno versions and templates remains experimental.

## Local account servers and clean-schema export

The optional [HolySong integration](../integrations/holysong/README.md) adds one
loopback server per account and isolated `-auto` commands. Account mode is opted
in with `SUNO_API_ACCOUNT_MODE=1` and a long `SUNO_API_LOCAL_TOKEN`. It requires
the matching local Bearer token and rejects request Cookie/Origin overrides.
The launcher binds 127.0.0.1; do not expose an instance to the network.

Additional Studio routes: GET `account` (authenticated account ID and API version),
`billing`, `check` (read-only CAPTCHA requirement check), and `alignment?id=UUID`
(raw exported word alignment). They use the same repo Suno session.

`export.state` is now optional: omitting it uses `studio-clean-template.json`,
with no account IDs, saved project data or effects. The API still validates
project ownership, completed source and duration. Explicit custom state remains
backward compatible. Internal-provider schema compatibility needs ongoing testing.

## Shared generation authentication and optional browser fallback

Legacy generation and Studio now share `SunoApi.getCaptcha()` and
`GenerationAuthGate`. Legacy calls retain their default browser behavior. Studio
calls pass an explicit policy from `SUNO_STUDIO_BROWSER_FALLBACK` (default false)
and use the Studio CAPTCHA-check endpoint. Without a challenge, both return a
null token without launching a browser. With a challenge and opt-in, Studio calls
the existing browser/2Captcha procedure and forwards its captured token in the
unchanged Studio generation payload. Failed/unknown checks stop without submission.

Browser generation interception is installed before UI interaction, matches both
legacy and web generation routes, and aborts their incidental POSTs. It validates
the captured account against the authenticated Suno account, rejects missing
tokens and unknown generation endpoint versions, and closes on success, error or
a 90-second timeout. Challenges are serialized per cached account instance; tokens
are not reused across callers. No paid generation POST is automatically retried.

Opt-in requires a working repository browser runtime, `TWOCAPTCHA_KEY` (solver
usage can incur charges), and the normal `BROWSER*` settings as needed. The
HolySong account launcher now forwards an explicit allowlist from each profile's
private credential env file. Restart that account server after changes. No runtime
or solver credentials are installed by this change; no browser was launched for
its tests. The optional flow inherits the existing `/create` UI selectors. Mocked
compatibility tests do not prove that a token captured from today's UI will be
accepted by the live Studio endpoint or that the selectors still match it.
