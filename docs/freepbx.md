# PBX setup (FreePBX / Asterisk)

wa2sip registers like a desk phone. For each bridge you need **one extension** for wa2sip, plus the
normal extensions of the phones that should ring for incoming WhatsApp calls.

## FreePBX 16/17

1. **Applications → Extensions → Add Extension → Add New SIP [chan_pjsip] Extension**
   - *User Extension*: e.g. `1009`
   - *Display Name*: e.g. `WhatsApp`
   - *Secret*: copy it, wa2sip needs it
2. Tab **Advanced**:
   - *Trust RPID/PAI*: **Yes**. Phones then show the WhatsApp caller (`WA Ali`, `+90532…`) instead
     of `WhatsApp <1009>`. FreePBX's dialplan may still apply the extension's own caller ID to
     internal calls on some versions. If so, keep **Announce the caller** on in the bridge: you hear
     who is calling when you pick up.
   - *DTMF Signaling*: RFC 4733 (the default) is best. wa2sip also understands SIP INFO and in-band
     tones.
   - *Max Contacts*: 1 is fine. Don't register a real phone on the same extension.
3. **Submit**, then **Apply Config**.
4. In wa2sip: **PBX & extensions → Add PBX** (FreePBX's IP, port 5060) → **Add extension**
   (`1009` + secret). The status turns **registered** within a few seconds.

Codecs: wa2sip offers G.711 A-law and µ-law. Make sure at least one is allowed for the extension
(they are by default).

### Firewall / network

- wa2sip uses UDP **5064** (SIP) and **17000-17199** (RTP) on the docker host (host networking).
  If the FreePBX firewall is on, add the wa2sip host as **Trusted** (Connectivity → Firewall →
  Networks) or allow those ports.
- If the docker host has several addresses, set `ADVERTISE_IP` to the one the PBX should send audio
  to.

## Plain Asterisk (pjsip.conf)

```ini
[1009]
type = endpoint
context = from-internal          ; a context that can dial your phones
disallow = all
allow = alaw,ulaw
dtmf_mode = rfc4733
direct_media = no
rtp_symmetric = yes
force_rport = yes
rewrite_contact = yes
trust_id_inbound = yes           ; show the WhatsApp caller from P-Asserted-Identity
auth = 1009
aors = 1009

[1009]
type = aor
max_contacts = 1
remove_existing = yes

[1009]
type = auth
auth_type = userpass
username = 1009
password = <secret>
```

`tools/test-pbx/conf/` is a complete, working test configuration (used by the e2e test).

## What the PBX sees

| Direction | SIP |
|---|---|
| You call `1009` | INVITE to wa2sip's registration (`sip:wa-xxxxxxxx@<host>:5064`); wa2sip answers at once and plays the menu |
| WhatsApp call comes in | wa2sip sends an INVITE **from 1009** to each ring target (`1001@pbx`), with `From: "WA Ali" <sip:1009@pbx>` and `P-Asserted-Identity: "WA Ali" <sip:+905321234567@pbx>` |

The ring targets can be anything extension 1009 may dial: extensions, ring groups, queues, even
outbound numbers (if your outbound routes allow it).
