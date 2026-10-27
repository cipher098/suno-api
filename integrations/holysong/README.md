# HolySong local account-server adapter

This optional integration provides **new commands only**:

```
holysong-generate-publish-batch-generic-auto
  → holysong-generate-publish-concurrent-generic-auto
    → one local suno-api server per account
    → Studio generate/status/export/download/alignment
    → existing HolySong publisher/uploader
```

The original `holysong-generate-publish-batch-generic` and concurrent commands are
not edited. The integration retains WEB source pinning, local Upbeat direction,
artwork generation, technical audio checks, existing publication authorization,
and production Live verification. All of those local HolySong dependencies must
already exist; this folder does not provision the app backend or grant rights.

## Installation

Use a separate clone of this fork's `feat/account-server-auto` branch and pin a
tested commit. Node 20+ and the existing HolySong Python/media runtime are needed.
Dependencies may be installed with `npm ci --ignore-scripts` to avoid installing
browser binaries; Studio operations never launch a browser or CAPTCHA solver.

```sh
npm ci --ignore-scripts
npm run build
python3 integrations/holysong/install.py --python /absolute/path/to/holysong/venv/bin/python

holysong-suno-account setup --account main \
  --env-file /absolute/path/to/private.env --port 3057 \
  --repo /absolute/path/to/this/clone --node /absolute/path/to/node
holysong-suno-account start --account main
holysong-generate-publish-concurrent-generic-auto --check
holysong-generate-publish-batch-generic-auto Genesis 1 1 --dry-run
```

The private env file supplies literal `SUNO_COOKIE=...` and
`SUNO_STUDIO_PROJECT_ID=...` values; it must be owned by the current user with mode
600. You can reuse the existing private credential file without editing it.
`setup --project-id UUID` overrides only the project selection. Do not commit
cookies, profiles, logs, HARs or generated media. Profiles live outside the repo,
under `~/.config/holysong/suno-accounts/<name>/profile.json`.

`start` runs the repo's production Next server on **127.0.0.1 only**, with a random
local API token and a fixed process-level Suno account. Incoming cookies/origins
are rejected in this opted-in mode; other existing repo API routes are also
protected by the token. The Studio methods share the repo's `SunoApi` authentication.
The server can expose other repository functions to authorized local callers, but
this pipeline uses only Studio operations. Do not invoke unrelated generation
routes that launch the repository's browser/CAPTCHA workflow.

The server starts automatically on the first online preflight/job if necessary.
It persists after the command finishes. `holysong-suno-account status --account
main` checks it; `stop` requests termination. Reboot recovery is lazy start on the
next invocation; no LaunchAgent or system service is installed.

## Multiple accounts

Give each different account its own private env, existing owned Studio project,
profile and port. For example:

```sh
holysong-suno-account setup --account second \
  --env-file /absolute/path/to/second.env --port 3058 \
  --repo /absolute/path/to/this/clone --node /absolute/path/to/node
holysong-suno-account start --account second
HOLYSONG_SUNO_ACCOUNT=second holysong-generate-publish-batch-generic-auto Genesis 1 1 --dry-run
# Actual generation and publication, when intended:
HOLYSONG_SUNO_ACCOUNT=second holysong-generate-publish-batch-generic-auto Genesis 1 1
```

Default account is `main`. The concurrent worker also accepts `--account second`.
Batch continuation `-c` is scoped to the selected account. Batch remains sequential;
separate processes/books can use worker concurrency. Account slots and chapter/book
locks coordinate with the original generic commands **on this machine**. Locks do
not coordinate multiple computers or unrelated callers to the local API server.
Avoid operating the same account from multiple machines simultaneously.

Jobs use a separate `~/.local/share/holysong-generate-publish-concurrent-generic-auto/`
root and account fingerprint. Existing old-flow jobs are not migrated or adopted.
A new auto job could therefore regenerate an old non-Live chapter once the old
process has stopped; inspect existing pending jobs before choosing chapters.

## Recovery and verification

Paid POST intents and responses are saved in the chapter's private Studio folder.
A known successful response is reused to poll the **same IDs** after interruption;
source selection is frozen before export. A request with an intent but no response
is **uncertain** and stops for reconciliation. It is never automatically resubmitted.
Provider moderation errors stop without fresh generation. A completed download
resumes missing alignment/publication only. Timings belong to the exported render
ID; raw provider words are preserved. Signed audio is downloaded by a separate
unauthenticated HTTP client. Timed lyrics and technical checks do not prove complete
Scripture fidelity or musical quality; asynchronous listening review remains needed.

The clean export schema is held in the repo, not captured from another account.
Eight minutes remains the tested adapter duration bound. Internal Suno interfaces,
account entitlement and model availability can change. No download-limit bypass
or official API support is claimed.

```sh
npm run test:auto
python3 -m unittest discover -s integrations/holysong -p test_auto.py -v
```

Tests are offline/mocked. An online `--check` verifies account/project access without
music generation, export, artwork or upload. A paid end-to-end song is a separate
validation; passing the tests/preflight does not certify it.
