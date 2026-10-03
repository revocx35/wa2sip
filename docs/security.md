# Security

## What is at stake

`/data` and the admin password give access to:

- the **WhatsApp sessions** (a linked device can read messages, see contacts and place calls),
- the **SIP secrets** of the extensions wa2sip registers.

Anyone who can use the web UI can list your WhatsApp contacts and make WhatsApp calls in your name.

## Web UI

- One admin password, scrypt-hashed (N=2^14). New passwords need 10 characters.
- Session cookie: HMAC-signed, bound to the password hash (a password change logs out all sessions),
  `HttpOnly`, `SameSite=strict`, `Secure` behind a trusted HTTPS proxy (`SECURE_COOKIES=auto`).
- Brute-force throttle per client IP (5 free failures, then 30 s doubling to 1 h) and per account
  (after 50 failures from anywhere, one try per lock period). Locked attempts get `429`.
- Cross-site requests are refused (Fetch Metadata), which also covers sibling subdomains and the
  cookie-less first setup.
- `X-Forwarded-For`/`-Proto` count only from `TRUSTED_PROXIES` (default: private networks), so
  clients can't spoof their IP past the throttle.
- Secrets (extension passwords) are never sent back by the API.
- The API token (Settings) is as powerful as the password. Regenerate it if it leaks.

Expose the UI to the internet only through an HTTPS reverse proxy (Nginx Proxy Manager, Caddy, …),
or better, only on your LAN or VPN.

## Chromium

WhatsApp Web renders content from anyone who messages you (images, link previews, stickers). A
renderer exploit would land in Chromium, so:

- **Chromium's sandbox stays on.** Docker's default seccomp profile blocks the user-namespace
  syscalls the sandbox needs, so the compose files apply `seccomp-chromium.json`: Docker's default
  profile plus `clone`/`clone3`/`unshare`/`setns`/`chroot` and similar. `CHROMIUM_SANDBOX=auto`
  checks at start-up whether namespaces work and only then omits `--no-sandbox`. Settings → *Browser
  sandbox* shows the result. On hosts that forbid unprivileged user namespaces (for example Ubuntu
  24.04 with `kernel.apparmor_restrict_unprivileged_userns=1` and Docker's AppArmor profile), it
  falls back to no sandbox with a warning in the log.
- The container runs as **uid 1000** with `no-new-privileges`, **all capabilities dropped** and a
  **read-only root filesystem** (tmpfs for `/tmp` and the home directory).
- The X display (Xvfb) listens only on its socket file inside the container (`-nolisten tcp
  -nolisten local`). With host networking an abstract socket would be reachable from the host.
- PulseAudio listens only on a socket in the container's `/tmp`.

## Network

- Host networking: wa2sip binds the web UI (8092/tcp), SIP (5064/udp) and RTP (17000-17199/udp) on
  all interfaces, and Piper on 127.0.0.1 only.
- Inbound SIP requests aren't authenticated (like most phones). Restrict 5064/udp and the RTP range
  to your PBX with a host firewall if the host is reachable from untrusted networks.
- Bridges can restrict which PBX callers may use them (*Allowed callers*) and require a **PIN**,
  both for calling out to WhatsApp and for answering incoming WhatsApp calls. Wrong PINs are counted
  per bridge across calls: after 10 in a row the PIN is locked (60 s, doubling up to an hour, the
  right PIN refused too), which keeps a 4-digit PIN from being guessed by redialling. PINs are
  compared in constant time and never written to the log or the call history. They are stored in
  `/data/config.json` and shown to the admin in the bridge editor.

## WhatsApp account

wa2sip uses the official WhatsApp Web client, the same as opening web.whatsapp.com. It doesn't
change your messages; it only turns off message sounds in its own session. Removing the device in
WhatsApp → Linked devices, or **Unlink** in wa2sip, ends its access at once.
