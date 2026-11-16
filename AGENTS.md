# AGENTS.md

Notes for AI agents (and humans) changing wa2sip. Stand-alone; CLAUDE.md has the longer history and
the debugging playbook, ARCHITECTURE.md the design.

## What it is

WhatsApp voice calls <-> SIP PBX. Per WhatsApp account, a Node agent runs the official WhatsApp Web
in Chromium (whatsapp-web.js + own in-page call control) as a linked device; Chromium's speaker/mic
are PulseAudio null sinks that the Python engine bridges to G.711 RTP on SIP extensions it registers.
Piper IVR, web UI (:8092 HTTP, HTTPS_PORT), telerouter integration API. One Docker Compose service.

## Layout

- `wa2sip/` Python engine: `engine.py` (routing), `sessions.py` (Inbound/Outbound/Direct/Telerouter
  sessions), `wa/` (`base.py` WaRuntime + state mapping, `agent.py` agent supervisor/protocol,
  `fake.py` FakeRuntime, `system.py` Xvfb/PulseAudio), `sip/`, `media/`, `web/` (FastAPI + vanilla
  JS UI), `integration.py`, `pin.py`.
- `agent/` Node: `index.js` (agent, JSON lines on stdin/stdout), `page.js` (runs inside WhatsApp
  Web), `readiness.js` (session state + liveness logic, unit-tested), `probe.js`, `loopback.js`,
  `test/`.
- `tests/` pytest (most run on FakeRuntime; `fake_agent.py` stands in for the Node agent),
  `tools/` (e2e with real Asterisk, audio loopback, test PBX), `docs/`, `deploy/`.

## Commands

```bash
docker build -t wa2sip-dev -f Dockerfile.dev .
docker run --rm -v $PWD:/src wa2sip-dev python -m pytest -q                       # Python, ~50 s
docker run --rm -v $PWD/agent:/a -w /a node:22-trixie-slim sh -c "npm ci && npm run check && npm test"
docker build -t wa2sip:dev .                                                       # image
docker run --rm --entrypoint node wa2sip:dev agent/probe.js --grep Voip           # WA Web modules
```
On the dev LXC run heavy jobs through `/usr/local/bin/heavy`. e2e and audio loopback: see CLAUDE.md.

## Conventions

- One asyncio loop; never block it. No per-sample Python: PulseAudio/pacat do G.711 + resampling.
- `WaRuntime` is the only WhatsApp interface; `FakeRuntime` must match the real one's states/errors.
- Agent stdout is protocol only. New command: `index.js#commands` + `AgentRuntime` + `page.js#api` + tests.
- page.js: plain ES2020, every `window.require` guarded, errors carry a `code`.
- Secrets are write-only in the API; PINs never reach logs. UI escapes everything with `esc()`.
- Env/API/behaviour changes: update README, `.env.example`, both compose files and docs together.
- Release: bump `wa2sip/__init__.py` and `agent/package.json`, tag `vX.Y.Z`, `gh release create`.
  Commits by the GitHub noreply identity, with the Co-Authored-By trailer.

## Never do

- Commit credentials, phone numbers, IPs of the owner's network (they go in git-ignored `CLAUDE.local.md`).
- Test-call real WhatsApp contacts on the owner's deployment. Inspect over CDP read-only.
- Use `client.logout()`/`client.destroy()` for unlinking (they close the browser).
- Decline (`rejectCall`) a WhatsApp call wa2sip never accepted, or decline an accepted one (use `endCall`).
- Restart the production container as the first diagnostic step: read logs, kernel log, the live tab.

## Gotchas

- `ready` is not forever: the WhatsApp Web renderer can be OOM-killed or freeze days later; then no
  events arrive and `page.evaluate` hangs. The agent restarts itself (page `error` event, 30 s
  liveness probe); keep that path when touching `index.js` (tests in `agent/test/readiness.test.js`).
- whatsapp-web.js events arrive in any order (late `loading_screen` after `ready`): readiness.js.
- `WAWebCallCollection.activeCall` only exists after the first call (probe `setActiveCall`).
- Xvfb with host networking needs `-nolisten local`; getUserMedia needs a secure context.
- Docker's default seccomp breaks the Chromium sandbox: use `seccomp-chromium.json`.
- Asterisk overwrites the From name unless `trust_id_inbound=yes` + PAI.
- The production container has a read-only rootfs: pipe debug scripts in (`docker exec -i … sh -c 'cat > /tmp/x.js'`).
