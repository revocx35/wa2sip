# WhatsApp Web internals used by wa2sip

wa2sip controls calls through WhatsApp Web's own JavaScript modules (`agent/page.js`). WhatsApp
doesn't document or promise these, so this page records what they are, where they were found and
how to find them again when WhatsApp changes them.

Checked against WhatsApp Web **2.3000.1049223755** (October 2026).

## How the modules are reached

WhatsApp Web uses Meta's module system: every module is defined with `__d("Name", deps, factory)`
and loaded with `window.require("Name")`. Some modules export a `default` (for example
`WAWebCallCollection`), so `page.js` reads `m.default ?? m`.

**The VoIP modules are defined before login**, which makes all of this checkable without an
account: about 12,500 modules on the logged-out page, among them a couple of hundred VoIP and call modules. The
real VoIP engine (`WAWebVoipStackInterfaceImpl`, a WASM stack) is loaded lazily on first use.

### Re-checking after a WhatsApp update

```bash
# are all modules/exports wa2sip needs still there? (weekly in CI: .github/workflows/waweb-probe.yml)
docker run --rm --entrypoint node ghcr.io/revocx35/wa2sip agent/probe.js
# list modules by name
docker run --rm --entrypoint node ghcr.io/revocx35/wa2sip agent/probe.js --grep Voip
# read a module's source (minified, but the names survive)
docker run --rm --entrypoint node ghcr.io/revocx35/wa2sip agent/probe.js --source useWAWebVoipCallHandlers
```

When something moved, read how WhatsApp's own UI does it now. The call buttons live in
`useWAWebVoipCallHandlers` (accept/reject/end), `WAWebExecApiCmdNewCall` and `WAWebVoipStartCall`
(placing calls). Then adapt `page.js` and its tests (`agent/test/page.test.js`).

## The API surface

### Observing calls: `WAWebCallCollection` (default export)

- An event-emitter collection of `WAWebCallModel`s: `get(id)`, `getModelsArray()`, `activeCall`
  (set via `setActiveCall`), `pendingOutgoingCall`, `processIncomingCall(...)`.
- `CallModel`: `id`, `peerJid` (a Wid, user or LID), `outgoing`, `isVideo`, `isGroup`, `groupJid`,
  `offerTime`, `getState()`, `peerBusy`, `callFailedReason`, `callLogResult`, `wasEverConnected`,
  `userEndedCall`.
- `getState()` returns `WAWebVoipWaCallEnums.CallState`: `None 0, Calling 1, PreacceptReceived 2,
  ReceivedCall 3, AcceptSent 4, AcceptReceived 5, CallActive 6, CallActiveElseWhere 7,
  ReceivedCallWithoutOffer 8, Rejoining 9, Link 10, ConnectedLonely 11, PreCalling 12,
  CallStateEnding 13, CallBCallStarting 14`.
- Call log results (`callLogResult`): `Undefined 0, Connected 1, Missed 2, Declined 3, Canceled 4,
  Unavailable 5, AcceptedElsewhere 6, MissedNotificationsMuted 7`.
- wa2sip **polls** every 200 ms instead of subscribing to events, so renamed event names can't
  silently break it. whatsapp-web.js hooks the collection's internal `Map.set` instead; that only
  sees new calls.

### Placing a call: `WAWebVoipStartCall.startWAWebVoipCall`

```js
startWAWebVoipCall(wid, isVideo, callFromUi = 0, r = 0, callId = null, { entryTrust })
```

- `entryTrust: 'user_gesture'` skips the *"Start a WhatsApp call with …?"* confirmation that deep
  links get (`WAWebVoipOutgoingCallConsent.hasOutgoingCallConsent`).
- `callFromUi` is `WAWebWamEnumCallFromUi.CALL_FROM_UI.*` (telemetry). wa2sip uses `CONVERSATION` (8).
- Inside: `ensureVoipInitialized` → device permission check (`checkVoipDevicePermissions(isVideo,
  null, signal)`) → signalling. The model shows up as `activeCall` with `outgoing: true` before the
  promise resolves.
- Numbers become Wids the way WhatsApp's own `call?phone=` deep link does:
  `WAWebQueryExistsJob.queryPhoneExists('+<digits>')` returns `{ wid, … }` or null (not on WhatsApp).
  Ids use `WAWebWidFactory.createWid('<user>@c.us' | '<lid>@lid')`.

### Answering, declining, hanging up: the VoIP stack interface

```js
const stack = await require('WAWebVoipStackInterface').getVoipStackInterface();   // stack.type === 'web'
await stack.acceptCall(audioEnabled /* true */, videoEnabled /* false */);
await stack.rejectCall();
await stack.endCall(require('WAWebVoipSignalingEnums').EndCallReason.Self /* 2 */, true);
```

These are the calls WhatsApp's own buttons make (`useWAWebVoipCallHandlers`). Before accepting,
the UI also calls `WAWebCallRingtone.stopCallRingtone()` and
`WAWebVoipAcquireMediaStream.checkVoipDevicePermissions(wantVideo, call)`; wa2sip does the same.
`EndCallReason`: `Unknown 0, Timeout 1, Self 2, RejectDoNotDisturb 3, RejectBlocked 4,
MicPermissionDenied 5, CameraPermissionDenied 6`.

### Gating: is calling enabled?

`WAWebVoipGatingUtils`:

- Browser prerequisites: `SharedArrayBuffer`, `Atomics`, `RTCPeerConnection` (the page is
  cross-origin isolated). They are all true in wa2sip's Chromium.
- AB props from the server after login: `enable_web_calling`, `enable_web_group_calling`, …
  (`WAWebABProps.getABPropConfigValue`).
- `isVoipDownloadEnabled()` decides whether the WASM stack may load.

**Diagnostics** in the web UI shows these values.

### Contacts and identity

- `WAWebContactCollection.ContactCollection.getModelsArray()`. Contact: `id` (Wid, `@c.us` or
  `@lid`), `name` (address book), `pushname`, `verifiedName`, `isAddressBookContact`/`isMyContact`,
  `isBusiness`, `phoneNumber` (Wid, on LID contacts).
- LID ↔ phone number: `WAWebLidMigrationUtils.toPn(wid)` / `toLid(wid)`. One-to-one chats and calls
  use LIDs more and more, so routing matches a contact by id **or** number.
- Own account: `WAWebUserPrefsMeUser.getMaybeMePnUser()`, `getMaybeMeLidUser()`,
  `getMaybeMeDisplayName()`.

### Sounds

`WAWebMuteCollection.MuteCollection.setGlobalSounds(false)` and `setOutgoingMessageSound(false)`
persist in this WhatsApp Web session's local prefs (not on the phone). wa2sip turns both off,
because message "dings" would play into bridged calls.

### Session

- `WAWebSocketModel.Socket.state`: `OPENING`, `PAIRING`, `UNPAIRED`, `UNPAIRED_IDLE`, `CONNECTED`, …
  (used by whatsapp-web.js).
- `Socket.logout()`: unlink like the menu item does. whatsapp-web.js's `client.logout()` would close
  the browser.

## What could not be verified without a linked phone

Logged out, the module **names and signatures** were checked in the source, and the browser
prerequisites hold. What happens with a real account (does `enable_web_calling` come back true,
does the lazily loaded stack accept these calls the same way, which audio devices WhatsApp Web
picks) needs a linked phone. The Diagnostics button and `WA_TRACE=true` are there for that first
real test.

## Alternatives considered

- **meowcaller** (Go, on whatsmeow; June 2026): a pure-Go WhatsApp VoIP stack with the MLow codec
  and a clean PCM API. In October 2026 it had open bugs on inbound audio for linked devices (#24,
  #36, #37, #40) and on outgoing calls to peers with WhatsApp Web open (#25). It also reimplements
  the protocol, which carries more ban risk than the official web client. Worth another look as an
  alternative `WaRuntime` once those are fixed (`wa/base.py` is the interface).
- **baileys-caller** (Sep 2026): wraps WhatsApp Web's WASM VoIP outside the browser; too new.
