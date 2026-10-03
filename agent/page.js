'use strict';
/**
 * Code that runs inside WhatsApp Web (page context). It installs window.__wa2sip:
 *
 *  - a call monitor that reports every state change of WhatsApp Web's call models
 *    (WAWebCallCollection) through the exposed binding window.__wa2sipEmit
 *  - call control (dial / accept / reject / hangup) through WhatsApp Web's own VoIP stack
 *  - contacts, number lookup, own identity and diagnostics
 *
 * Everything goes through WhatsApp Web's module registry (window.require). Module and
 * function names were taken from WhatsApp Web 2.3000.x (Oct 2026); see docs/whatsapp-web-internals.md.
 * Each lookup is defensive so a renamed module degrades into a clear error instead of a crash.
 */

function installPageApi() {
    if (window.__wa2sip && window.__wa2sip.version === 1) return 'already';

    const R = (name) => {
        try {
            return window.require(name);
        } catch (e) {
            return null;
        }
    };
    const D = (name) => {
        const m = R(name);
        return m && m.default ? m.default : m;
    };
    const fail = (code, message) => {
        const e = new Error(message || code);
        e.code = code;
        return e;
    };
    const emit = (obj) => {
        try {
            if (typeof window.__wa2sipEmit === 'function') window.__wa2sipEmit(JSON.stringify(obj));
        } catch (e) {
            /* binding gone during navigation */
        }
    };
    const str = (w) => {
        if (!w) return null;
        if (typeof w === 'string') return w;
        try {
            if (w._serialized) return w._serialized;
            if (w.$1) return w.$1;
            return w.toString();
        } catch (e) {
            return null;
        }
    };
    const toWid = (s) => {
        if (!s || typeof s !== 'string') return s;
        try {
            return R('WAWebWidFactory').createWid(s);
        } catch (e) {
            return null;
        }
    };

    function contactFor(keys) {
        const coll = R('WAWebContactCollection');
        const CC = coll && coll.ContactCollection;
        if (!CC) return null;
        for (const k of keys) {
            if (!k) continue;
            try {
                const c = CC.get(k) || CC.get(toWid(k));
                if (c) return c;
            } catch (e) {
                /* next */
            }
        }
        return null;
    }

    function nameOf(c) {
        if (!c) return null;
        return c.name || c.verifiedName || c.pushname || c.notifyName || null;
    }

    function peerInfo(w) {
        if (!w) return {};
        const s = str(w);
        const wid = typeof w === 'string' ? toWid(w) : w;
        const LU = R('WAWebLidMigrationUtils');
        let lid = null;
        let pn = null;
        try {
            if (s && s.endsWith('@lid')) {
                lid = s;
                pn = str(LU && LU.toPn(wid));
            } else {
                pn = s;
                lid = str(LU && LU.toLid(wid));
            }
        } catch (e) {
            /* no mapping */
        }
        const c = contactFor([s, pn, lid]);
        let number = pn ? pn.split('@')[0] : null;
        if (!number && c && c.phoneNumber) number = str(c.phoneNumber).split('@')[0];
        return { jid: s, lid, pn_jid: pn, number, name: nameOf(c) };
    }

    function snap(m) {
        let state = null;
        try {
            state = m.getState();
        } catch (e) {
            /* keep null */
        }
        return {
            id: m.id,
            state,
            outgoing: !!m.outgoing,
            isVideo: !!m.isVideo,
            isGroup: !!(m.isGroup || m.groupJid),
            peer: peerInfo(m.peerJid),
            peerBusy: !!m.peerBusy,
            failedReason: m.callFailedReason == null ? null : String(m.callFailedReason),
            logResult: m.callLogResult == null ? null : m.callLogResult,
            everConnected: !!m.wasEverConnected,
            userEnded: !!m.userEndedCall,
            offerTime: m.offerTime || null,
        };
    }

    // -- call monitor --------------------------------------------------------------------
    const seen = new Map();
    function tick() {
        const CC = D('WAWebCallCollection');
        if (!CC) return;
        const models = new Set();
        try {
            for (const m of CC.getModelsArray()) models.add(m);
        } catch (e) {
            /* older builds */
        }
        if (CC.activeCall) models.add(CC.activeCall);
        for (const m of models) {
            if (!m || !m.id) continue;
            let st = null;
            try {
                st = m.getState();
            } catch (e) {
                /* ignore */
            }
            const rec = seen.get(m.id);
            if (!rec || rec.state !== st || rec.model !== m) {
                seen.set(m.id, { model: m, state: st });
                emit({ type: 'call', call: snap(m) });
            }
        }
        for (const [id, rec] of seen) {
            if (!models.has(rec.model)) {
                seen.delete(id);
                const s = snap(rec.model);
                s.removed = true;
                emit({ type: 'call', call: s });
            }
        }
    }
    const timer = setInterval(tick, 200);

    // -- call control ------------------------------------------------------------------------
    async function stack() {
        const SI = R('WAWebVoipStackInterface');
        if (!SI) throw fail('unsupported', 'WhatsApp Web has no VoIP stack (WAWebVoipStackInterface missing)');
        const s = await SI.getVoipStackInterface();
        if (!s) throw fail('unsupported', 'calling is not enabled for this WhatsApp Web session');
        return s;
    }

    function activeCall(callId) {
        const CC = D('WAWebCallCollection');
        const call = CC && CC.activeCall;
        if (!call || (callId && call.id !== callId)) throw fail('no_such_call', 'no such WhatsApp call');
        return call;
    }

    async function resolveTarget(to) {
        if (to.includes('@')) {
            const wid = toWid(to);
            if (!wid) throw fail('bad_target', 'invalid WhatsApp id ' + to);
            return wid;
        }
        const digits = to.replace(/\D/g, '');
        if (digits.length < 6) throw fail('bad_target', 'invalid phone number');
        const QE = R('WAWebQueryExistsJob');
        const res = await QE.queryPhoneExists('+' + digits);
        if (!res || !res.wid) throw fail('not_on_whatsapp', 'this number is not on WhatsApp');
        return res.wid;
    }

    async function dial(to) {
        const CC = D('WAWebCallCollection');
        if (CC && CC.activeCall) throw fail('busy', 'WhatsApp is already in a call');
        const SC = R('WAWebVoipStartCall');
        if (!SC || !SC.startWAWebVoipCall) throw fail('unsupported', 'WAWebVoipStartCall missing');
        const wid = await resolveTarget(to);
        const enums = R('WAWebWamEnumCallFromUi');
        const fromUi = (enums && enums.CALL_FROM_UI && enums.CALL_FROM_UI.CONVERSATION) || 8;
        let startError = null;
        let doneAt = 0;
        // resolves once WhatsApp has set the call up (or gave up); the call model appears before that
        SC.startWAWebVoipCall(wid, false, fromUi, 0, null, { entryTrust: 'user_gesture' }).then(
            () => {
                doneAt = Date.now();
            },
            (e) => {
                doneAt = Date.now();
                startError = e || new Error('unknown error');
            },
        );
        const deadline = Date.now() + 30000;
        while (Date.now() < deadline) {
            const c = CC && CC.activeCall;
            if (c && c.outgoing) return snap(c);
            if (startError) throw fail('failed', 'WhatsApp could not start the call: ' + (startError.message || startError));
            if (doneAt && Date.now() - doneAt > 3000) {
                throw fail('failed', 'WhatsApp did not start the call (microphone or calling unavailable?)');
            }
            await new Promise((r) => setTimeout(r, 100));
        }
        throw fail('timeout', 'WhatsApp did not start the call in time');
    }

    async function accept(callId) {
        const call = activeCall(callId);
        try {
            R('WAWebCallRingtone').stopCallRingtone();
        } catch (e) {
            /* optional */
        }
        const AMS = R('WAWebVoipAcquireMediaStream');
        if (AMS && AMS.checkVoipDevicePermissions) {
            const ok = await AMS.checkVoipDevicePermissions(false, call);
            if (ok === false) throw fail('no_microphone', 'WhatsApp Web has no microphone permission');
        }
        const s = await stack();
        await s.acceptCall(true, false);
        return snap(call);
    }

    async function reject(callId) {
        const call = activeCall(callId);
        call.userEndedCall = true;
        const s = await stack();
        await s.rejectCall();
        return true;
    }

    async function hangup(callId) {
        const call = activeCall(callId);
        call.userEndedCall = true;
        const SE = R('WAWebVoipSignalingEnums');
        const self = (SE && SE.EndCallReason && SE.EndCallReason.Self) || 2;
        const s = await stack();
        await s.endCall(self, true);
        return true;
    }

    // -- contacts & identity ---------------------------------------------------------------------
    function contacts() {
        const coll = R('WAWebContactCollection');
        const CC = coll && coll.ContactCollection;
        if (!CC) throw fail('unsupported', 'WAWebContactCollection missing');
        const LU = R('WAWebLidMigrationUtils');
        const byKey = new Map();
        for (const c of CC.getModelsArray()) {
            try {
                const id = str(c.id);
                if (!id || !(id.endsWith('@c.us') || id.endsWith('@lid'))) continue;
                if (c.isMe) continue;
                const isMy = !!(c.isAddressBookContact || c.isMyContact);
                const name = c.name || null;
                const pushname = c.pushname || c.verifiedName || null;
                if (!isMy && !name && !pushname) continue;
                let number = null;
                let lid = null;
                if (id.endsWith('@c.us')) {
                    number = id.split('@')[0];
                    try {
                        lid = str(LU.toLid(c.id));
                    } catch (e) {
                        /* none */
                    }
                } else {
                    lid = id;
                    if (c.phoneNumber) number = str(c.phoneNumber).split('@')[0];
                    if (!number) {
                        try {
                            const pn = str(LU.toPn(c.id));
                            if (pn) number = pn.split('@')[0];
                        } catch (e) {
                            /* none */
                        }
                    }
                }
                const entry = {
                    id: number ? number + '@c.us' : id,
                    lid,
                    number,
                    name,
                    pushname,
                    isMyContact: isMy,
                    isBusiness: !!c.isBusiness,
                };
                const key = number || id;
                const prev = byKey.get(key);
                if (!prev || (!prev.name && entry.name)) byKey.set(key, { ...prev, ...entry, lid: entry.lid || (prev && prev.lid) });
            } catch (e) {
                /* skip broken contact */
            }
        }
        return Array.from(byKey.values());
    }

    async function lookup(number) {
        const digits = String(number).replace(/\D/g, '');
        if (digits.length < 6) return null;
        const res = await R('WAWebQueryExistsJob').queryPhoneExists('+' + digits);
        if (!res || !res.wid) return null;
        const jid = str(res.wid);
        const c = contactFor([jid, digits + '@c.us']);
        return { jid, number: digits, name: nameOf(c) };
    }

    function me() {
        const U = R('WAWebUserPrefsMeUser');
        if (!U) return null;
        let pn = null;
        let lid = null;
        let name = null;
        try {
            pn = U.getMaybeMePnUser();
        } catch (e) {
            /* none */
        }
        try {
            lid = U.getMaybeMeLidUser();
        } catch (e) {
            /* none */
        }
        try {
            name = U.getMaybeMeDisplayName ? U.getMaybeMeDisplayName() : null;
        } catch (e) {
            /* none */
        }
        const id = str(pn);
        return { id, lid: str(lid), number: id ? id.split('@')[0] : null, name: name || null };
    }

    function diag() {
        const abp = R('WAWebABProps');
        const prop = (k) => {
            try {
                return abp ? abp.getABPropConfigValue(k) : null;
            } catch (e) {
                return null;
            }
        };
        const gating = R('WAWebVoipGatingUtils');
        const g = (fn) => {
            try {
                return gating && gating[fn] ? gating[fn]() : null;
            } catch (e) {
                return 'error: ' + e.message;
            }
        };
        const CC = D('WAWebCallCollection');
        let socket = null;
        try {
            socket = R('WAWebSocketModel').Socket.state;
        } catch (e) {
            /* none */
        }
        const mods = {};
        for (const n of [
            'WAWebCallCollection',
            'WAWebVoipStartCall',
            'WAWebVoipStackInterface',
            'WAWebVoipAcquireMediaStream',
            'WAWebVoipSignalingEnums',
            'WAWebContactCollection',
            'WAWebQueryExistsJob',
        ])
            mods[n] = !!R(n);
        return {
            waVersion: (window.Debug && window.Debug.VERSION) || null,
            userAgent: navigator.userAgent,
            visibility: document.visibilityState,
            socket,
            sharedArrayBuffer: typeof window.SharedArrayBuffer !== 'undefined',
            crossOriginIsolated: !!window.crossOriginIsolated,
            rtcPeerConnection: typeof window.RTCPeerConnection !== 'undefined',
            enable_web_calling: prop('enable_web_calling'),
            voipDownloadEnabled: g('isVoipDownloadEnabled'),
            modules: mods,
            activeCall: CC && CC.activeCall ? snap(CC.activeCall) : null,
        };
    }

    // -- user-gesture runner: lets the agent run an action inside a trusted click ------------------
    const results = {};
    let armed = null;
    function button() {
        let b = document.getElementById('__wa2sip_gesture');
        if (!b) {
            b = document.createElement('button');
            b.id = '__wa2sip_gesture';
            b.setAttribute('aria-hidden', 'true');
            b.tabIndex = -1;
            b.style.cssText =
                'position:fixed;left:0;top:0;width:6px;height:6px;opacity:0.01;z-index:2147483647;border:0;padding:0;margin:0;background:transparent';
            b.addEventListener('click', () => {
                const job = armed;
                armed = null;
                if (job) run(job);
            });
            document.body.appendChild(b);
        }
        return b;
    }
    function run(job) {
        Promise.resolve()
            .then(() => api[job.fn](...job.args))
            .then(
                (value) => {
                    results[job.token] = { done: true, value };
                },
                (e) => {
                    results[job.token] = { done: true, error: String((e && e.message) || e), code: (e && e.code) || 'error' };
                },
            );
    }
    function arm(fn, args, token) {
        button();
        results[token] = { done: false };
        armed = { fn, args, token };
        return true;
    }
    function runArmed(token) {
        if (armed && armed.token === token) {
            const job = armed;
            armed = null;
            run(job);
        }
        return true;
    }
    function result(token) {
        const r = results[token];
        if (r && r.done) delete results[token];
        return r || null;
    }

    // message "ding" sounds would leak into bridged calls (they play on the same audio device);
    // these settings only affect this WhatsApp Web session, not the phone
    function quiet() {
        const M = R('WAWebMuteCollection');
        const MC = M && M.MuteCollection;
        if (!MC) return false;
        let changed = false;
        if (MC.getGlobalSounds && MC.getGlobalSounds() !== false) {
            MC.setGlobalSounds(false);
            changed = true;
        }
        if (MC.getOutgoingMessageSound && MC.getOutgoingMessageSound() !== false) {
            MC.setOutgoingMessageSound(false);
            changed = true;
        }
        return changed;
    }

    const api = { dial, accept, reject, hangup, contacts, lookup, me, diag, quiet };
    window.__wa2sip = { version: 1, api, arm, runArmed, result, tick, stop: () => clearInterval(timer) };
    tick();
    return 'installed';
}

/** Turns off the browser's echo cancellation, noise suppression and AGC on the virtual microphone. */
function rawMicrophone() {
    const md = navigator.mediaDevices;
    if (!md || !md.getUserMedia || md.__wa2sipRaw) return;
    const orig = md.getUserMedia.bind(md);
    md.getUserMedia = function (constraints) {
        try {
            if (constraints && constraints.audio) {
                const a = typeof constraints.audio === 'object' ? Object.assign({}, constraints.audio) : {};
                a.echoCancellation = false;
                a.noiseSuppression = false;
                a.autoGainControl = false;
                constraints = Object.assign({}, constraints, { audio: a });
            }
        } catch (e) {
            /* keep the original constraints */
        }
        return orig(constraints);
    };
    md.__wa2sipRaw = true;
}

module.exports = { installPageApi, rawMicrophone };
