# CLAUDE.md

Guide for AI assistants (and humans) working on this repository. Read it fully before changing call,
audio or WhatsApp code.

## What this is

**wa2sip** bridges WhatsApp voice calls to a SIP PBX. Per WhatsApp account it runs the **official
WhatsApp Web** in Chromium as a linked device, and drives WhatsApp Web's own calling (available
since July 2026) through its internal modules. Chromium's speaker/mic are PulseAudio virtual
devices that wa2sip connects to SIP/RTP calls on extensions it registers on the PBX. A Piper-voiced
IVR lists the bridge's contacts when you call the extension. One Docker Compose service, web UI on
:8092.

Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing call or media code (process model, call flows,
audio pipeline, routing). WhatsApp Web module details: [docs/whatsapp-web-internals.md](docs/whatsapp-web-internals.md).

## Status: what is verified and what isn't (v0.2.0, 2026-10-03)

Verified on the dev host (and in CI):
- PBX side end to end against Asterisk 20 (`tools/e2e_fake.py`): registration, menu, RFC 4733 DTMF,
  dial-a-number, ring-back, forked ringing, PAI caller ID with `trust_id_inbound`, CANCEL, BYE, and
  bridge PINs in both directions (v0.2.0).
- Audio path Chromium ↔ PulseAudio ↔ parec/pacat (`tools/audio_loopback.py`), sandbox on and off:
  ~100 ms, no level loss.
- Real WhatsApp Web loads, shows a QR (also in the UI), the call monitor installs, and every module
  wa2sip uses exists (`agent/probe.js`). SharedArrayBuffer/crossOriginIsolated/RTCPeerConnection are
  all true.

**Not verified: real WhatsApp calls.** No phone was linked in the development session. The call
control in `agent/page.js` follows WhatsApp Web's own button handlers as read from its source, but
it has never run against a logged-in session. When the owner first tests with a real account,
expect to debug this. See the playbook below.

## Debugging playbook for the first real calls

Ask the owner for: Logs with `WA_TRACE=true` and `LOG_LEVEL=DEBUG`, the **Diagnostics** JSON, and
a **Browser view** screenshot during a call. Then check in order:

1. **`enable_web_calling` / `voipDownloadEnabled` false** (Diagnostics): WhatsApp hasn't enabled
   web calling for this session. Check whether calling works for that account in a normal browser.
   Nothing to fix in code then.
2. **No `call … incoming` log for an incoming call**: the monitor doesn't see the model. Check
   `WAWebCallCollection` (default export? `getModelsArray`?) with `agent/probe.js --source
   WAWebCallCollection`; maybe calls now live in `WAWebVoipOngoingCallCollection`.
3. **accept fails**: compare `useWAWebVoipCallHandlers` (`probe.js --source`) with `accept()` in
   page.js. `acceptCall(audio, video)` was the signature on 2026-10-03.
4. **dial fails or a confirmation popup shows** (Browser view): `startWAWebVoipCall` args/consent
   changed (`WAWebVoipOutgoingCallConsent`, `entryTrust`). The gesture click is in
   `index.js#gestureCall`.
5. **Connected but no audio**: WhatsApp Web may have picked another mic/speaker. In Browser view
   open the call's device menu. Chromium's default devices come from `PULSE_SINK`/`PULSE_SOURCE`.
   The pacat counters on the call card show whether audio flows on our side. Check
   `pactl --server=unix:/tmp/wa2sip/pulse/native list sink-inputs` / `source-outputs` inside the
   container during a call: Chromium must have a sink-input on `wa_<id>_spk` and a source-output on
   `wa_<id>_in`.
6. **Call works once, then not**: WhatsApp Web may open the call UI in a popup/PiP window. Look for
   new targets (`WA_TRACE` logs `new browser window`).

Always fix page.js together with `agent/test/page.test.js` (fake module registry mirrors the real
shapes) and update docs/whatsapp-web-internals.md with the WhatsApp version you checked.

## Commands

```bash
# run the stack (host networking)
cp .env.example .env && docker compose up -d --build
docker logs -f wa2sip

# Python tests (host has no pip/venv: use the dev image)
docker build -t wa2sip-dev -f Dockerfile.dev .
docker run --rm -v $PWD:/src wa2sip-dev python -m pytest -q              # ~35 s

# agent (page.js) tests and syntax checks
docker run --rm -v $PWD/agent:/a -w /a node:22-trixie-slim sh -c "npm ci --no-audit --no-fund && npm run check && npm test"

# end-to-end: real Asterisk + wa2sip (simulated WhatsApp) + SIP test phones
docker build -t wa2sip:dev . && docker build -t wa2sip-test-pbx tools/test-pbx
docker run -d --rm --name wa2sip-pbx --network host wa2sip-test-pbx
docker run -d --rm --name wa2sip-e2e --network host -e WA2SIP_WA_DRIVER=fake -e WA2SIP_ADMIN_PASSWORD=e2e-password-1 wa2sip:dev
docker run --rm --network host -v $PWD:/src -w /src wa2sip:dev python tools/e2e_fake.py

# audio path through real Chromium + PulseAudio (sandbox needs the seccomp profile)
docker run --rm --security-opt seccomp=$PWD/seccomp-chromium.json wa2sip:dev python tools/audio_loopback.py

# WhatsApp Web compatibility (needs internet, no account)
docker run --rm --entrypoint node wa2sip:dev agent/probe.js [--grep Voip | --source <Module>]

# UI screenshots / smoke test: Playwright image (mcr.microsoft.com/playwright/python:v1.55.0-noble,
# `pip install playwright==1.55.0` first) against a running instance on :8092
```

Ports used on the dev host by tests: 8092 (web), 5064 (wa2sip SIP), 5160 (test Asterisk), 5170-5172
(test phones), 17000-17199 + 17500-17640 (RTP), 19000-19199 (Asterisk RTP). The dev host also runs
other projects with host networking (cam2sip uses 8090/8443/5062/16000-16199, web-ip-phone 5070).

Local test-environment details (real PBX, real phone numbers, credentials) belong in
`CLAUDE.local.md` (git-ignored). **Never commit credentials or phone numbers.**

## Layout

ARCHITECTURE.md §2 has the full code map. In short: `wa2sip/` (Python engine: `engine.py`, `sessions.py`,
`wa/`, `sip/`, `media/`, `web/`), `agent/` (Node: `index.js` agent, `page.js` in-page code,
`probe.js`, `loopback.js`), `tests/`, `tools/` (e2e, audio loopback, test PBX), `docs/`.

## Conventions

- One asyncio loop (uvicorn's) for web, SIP, RTP and sessions. Never block it. Media callbacks
  (`on_packet`, `on_audio`) run synchronously and must stay cheap. No per-sample Python on the hot
  path: PulseAudio does G.711 and resampling (pacat `--format=alaw|ulaw --rate=8000`).
- G.711 only on the SIP side. The PulsePipe is opened with the SIP call's codec.
- `WaRuntime` (`wa/base.py`) is the only interface the engine uses for WhatsApp. `FakeRuntime` must
  keep behaving like the real one (same states, same `WaError` codes), because most tests run on it.
- WhatsApp call states are wa2sip strings (`incoming`, `calling`, `ringing`, `connecting`, `active`,
  `elsewhere`, `ended`); the mapping from WhatsApp's enum lives only in `wa/base.py`.
- Agent protocol: stdout is protocol only. Libraries' console output is redirected to stderr. Add a
  command in `index.js#commands` + `AgentRuntime` + (if in-page) `page.js#api` + tests.
- page.js runs inside WhatsApp Web: plain ES2020, no closures over Node values, every
  `window.require` guarded (`R()`/`D()`), errors carry a `code`.
- Secrets (`password`) are write-only in the API (`redact()`/`merge()` in web/app.py). Bridge PINs
  are visible to the admin, but must never reach logs or history: DTMF logs mask digits in the
  `pin` phase, and `ask_pin` logs only "wrong PIN"/"no PIN entered".
- UI: vanilla JS, no build step, escape everything with `esc()`. Keep `menuCodes()` in app.js equal
  to `Bridge.menu_codes()`.
- When you change env vars, API or behaviour, update README.md, `.env.example`, both compose files
  (`docker-compose.yml`, `deploy/docker-compose.yml`) and the docs.
- Commits end with the Co-Authored-By trailer. CI must stay green: it publishes the GHCR image on `main`.

## Hard-won facts (don't re-learn these)

- **WhatsApp Web defines its VoIP modules before login**, so everything except the lazily loaded
  `WAWebVoipStackInterfaceImpl` can be inspected without an account (`agent/probe.js`). That's how
  page.js was written.
- `WAWebCallCollection.activeCall` doesn't exist until the first call (`setActiveCall` creates it):
  probe for `setActiveCall`, not `activeCall`.
- **Xvfb + host networking**: Xvfb also listens on the *abstract* socket `@/tmp/.X11-unix/X99`,
  which lives in the network namespace, i.e. on the host. `-nolisten local` turns it off (checked
  with `/proc/net/unix`). Xvfb as non-root can't create `/tmp/.X11-unix`; system.py creates it.
- **getUserMedia needs a secure context**: `about:blank`/`data:` pages have no
  `navigator.mediaDevices`. loopback.js serves its page from `http://127.0.0.1`.
- Docker's default seccomp blocks user namespaces → Chromium sandbox fails. `seccomp-chromium.json`
  (same file as wa_logger) fixes it; `CHROMIUM_SANDBOX=auto` probes with `unshare -Upn` at start.
  Works with `no-new-privileges`, `cap_drop: ALL` and `read_only`.
- **Asterisk replaces the From display name with the endpoint's `callerid`** for calls from that
  endpoint, unless `trust_id_inbound=yes` and a P-Asserted-Identity is present. That's why
  `Account.dial()` sends PAI. FreePBX: Trust RPID/PAI.
- An in-band-DTMF phone calling through Asterisk reaches wa2sip as RFC 4733: Asterisk converts it.
  The in-band detector is only exercised by unit tests.
- A sine at -10 dBFS peak measures -13 dBFS RMS. That's not a 3 dB loss in the audio path.
- `pacat` record from a null sink's monitor delivers a steady 8 kHz stream even when Chromium plays
  nothing (silence), as long as the sink isn't suspended (`-n` = no `module-suspend-on-idle`).
- When the PBX caller hangs up, the outbound session must be **cancelled** (not polled). Otherwise
  history and cleanup wait for the current prompt or digit timeout (up to the menu length + 6 s).
- PIN entry must accept **type-ahead**: people start typing during the announcement/prompt.
  Clearing the key queue before the first try lost the first digits (found by test_pin).
- `Call.hangup()` on a ringing outbound call sends **one** CANCEL. Two cleanup paths used to send two;
  Asterisk ignores the repeated CANCEL, so the second `hangup()` waited 5 s for an answer and
  sessions lingered (found by the e2e PIN step; `test_second_hangup_while_cancelling_does_not_wait`).
- InboundSession cleanup must not decline a WhatsApp call it never accepted (e.g. the extension is
  not registered): leave it ringing on the phone. An accepted call is ended with `endCall`
  (`hangup`), never `rejectCall`.
- whatsapp-web.js is pinned to a GitHub commit (tarball URL in agent/package.json) because npm's
  1.34.7 predates fixes for WhatsApp Web's 2026 changes (`id._serialized` → `$1`, LIDs). The owner's
  wa_logger notes (docs/wwebjs-notes.md there) list more wwebjs pitfalls: `destroy()`/`logout()`
  close the browser, `ready` may never fire, `getChats()` breaks on current builds.
- Piper downloads the default voice (`en_US-lessac-medium`, 63 MB) at the first start. Tests never
  download (conftest makes the catalog unavailable).

## Release

- Pushes to `main` run CI (python, agent, e2e) and build the multi-arch image
  `ghcr.io/revocx35/wa2sip` (`latest`, `sha-…`). Tags `v*` add semver tags. A weekly job runs
  `agent/probe.js` against the live WhatsApp Web.
- Release: bump `wa2sip/__init__.py` `__version__` and `agent/package.json` version, commit, tag
  `vX.Y.Z`, push the tag, then `gh release create vX.Y.Z` with notes.
