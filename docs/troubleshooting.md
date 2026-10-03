# Troubleshooting

Start with **Logs** in the web UI (filter for `wa2sip.wa` or `session`). Set `LOG_LEVEL=DEBUG`,
`WA_TRACE=true` (every WhatsApp call state) or `SIP_TRACE=true` (every SIP message) in `.env` and
run `docker compose up -d` again for more.

## WhatsApp

| Symptom | What to check |
|---|---|
| No QR code, state stays *loading* | **WhatsApp → Browser view** shows what Chromium displays. `docker logs wa2sip` should say `WhatsApp agent started`. On a slow host the first start can take a minute. |
| *failed / agent exited* in a loop | Usually Chromium can't start. Run `docker compose logs`. With `CHROMIUM_SANDBOX=auto`, wa2sip falls back to no sandbox when user namespaces are blocked. Check that `seccomp-chromium.json` sits next to the compose file. |
| QR scanned, but no *connected* | Wait: the first sync can take a few minutes. **Browser view** shows the progress. |
| *disconnected* | The phone was offline for a long time, or the device was removed in WhatsApp → Linked devices. Unlink/Delete in wa2sip and link again. |
| Calls aren't bridged at all | **WhatsApp → Diagnostics**: `enable_web_calling` must be **true** after linking, and `voipDownloadEnabled` true. If it is false, WhatsApp hasn't enabled web calling for this session or account (rollout). Check with a normal browser on web.whatsapp.com whether calling works for your account there. |
| Contacts list is empty | Contacts arrive during the first sync; press **Refresh** later. You can always add numbers by hand. |

## Incoming WhatsApp calls don't ring the PBX

1. Logs: `WhatsApp call <id> from <name>: incoming` must appear. If not, WhatsApp Web didn't see the
   call: check **Browser view** while someone calls, and the diagnostics above.
2. Then `WhatsApp call from … -> ringing 1001 via bridge …`. If you see `not bridged: caller is not
   bridged` instead, add the contact to a bridge or tick *whole WhatsApp account*. With `extension
   … is not registered`, fix the PBX side first.
3. The ring targets get an INVITE from the bridge's extension. If the PBX rejects it (`403`, `404`),
   check that this extension may dial those numbers (FreePBX: its context/class of service).

## Calling the extension doesn't work

- Status of the extension in **PBX & extensions** must be **registered**. `401`/`403` means a
  wrong secret or auth user; *timeout* means the PBX isn't reachable on UDP, or a firewall is in
  the way.
- You hear the menu but key presses do nothing: set the extension's DTMF mode to RFC 4733 on the
  PBX. wa2sip understands RFC 4733, SIP INFO and in-band tones, but some PBX setups strip DTMF.
- *"WhatsApp is not connected right now"*: the bridge's WhatsApp account isn't linked/ready.

## One-way or no audio

- **PBX side**: RTP must flow between the PBX and the docker host on 17000-17199/UDP. The **Calls**
  page shows RTP packet counts in both directions. 0 *in* means the PBX doesn't send to us (wrong
  `ADVERTISE_IP`, firewall).
- **WhatsApp side**: the active call card shows *s from WhatsApp / s to WhatsApp*. If those
  counters grow but you hear silence, WhatsApp Web isn't using the virtual devices. Run the audio
  self-test:

  ```bash
  docker run --rm --security-opt seccomp=seccomp-chromium.json ghcr.io/revocx35/wa2sip python tools/audio_loopback.py
  ```

  It should print `OK` and a tone level around -13 dBFS. If it passes, compare WhatsApp Web's
  microphone/speaker choice in its call settings (**Browser view** during a call).

## The voice sounds robotic

The natural voice wasn't downloaded yet, so espeak-ng filled in. Check **Voices**: the voice should be
listed as installed. The server needs internet access once per voice (huggingface.co).

## Collecting information for a bug report

- Logs page (or `docker logs wa2sip`), ideally with `WA_TRACE=true`
- WhatsApp → Diagnostics output
- A Browser view screenshot during the problem
- `docker run --rm --entrypoint node ghcr.io/revocx35/wa2sip agent/probe.js` (does WhatsApp Web
  still have the internals wa2sip uses?)

Remove phone numbers and names before posting publicly.
