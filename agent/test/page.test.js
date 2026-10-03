'use strict';
/**
 * Tests for the in-page API (page.js) against a fake WhatsApp Web module registry that mimics
 * the real modules' shapes (see docs/whatsapp-web-internals.md). Run: node --test agent/test
 */
const test = require('node:test');
const assert = require('node:assert');
const { installPageApi, rawMicrophone } = require('../page');

class Wid {
    constructor(s) {
        this.s = s;
    }
    toString() {
        return this.s;
    }
}

function setup() {
    const emitted = [];
    const started = [];
    const CallCollection = {
        activeCall: null,
        models: [],
        pendingOutgoingCall: null,
        getModelsArray() {
            return this.models;
        },
    };
    const makeCall = (id, state, extra = {}) => ({
        id,
        _s: state,
        getState() {
            return this._s;
        },
        peerJid: new Wid('491701111001@c.us'),
        outgoing: false,
        ...extra,
    });
    const stack = {
        type: 'web',
        accepted: [],
        rejected: 0,
        ended: [],
        async acceptCall(audio, video) {
            this.accepted.push([audio, video]);
        },
        async rejectCall() {
            this.rejected++;
        },
        async endCall(reason, x) {
            this.ended.push([reason, x]);
        },
    };
    const contacts = [
        { id: new Wid('491701111001@c.us'), name: 'Alice', pushname: 'Ali', isAddressBookContact: true },
        { id: new Wid('99887766@lid'), phoneNumber: new Wid('491701111001@c.us'), pushname: 'Ali' },
        { id: new Wid('555@lid'), pushname: 'Stranger' },
        { id: new Wid('123-456@g.us'), name: 'A group' },
        { id: new Wid('490000@c.us'), name: 'Me', isMe: true },
    ];
    const modules = {
        WAWebCallCollection: { default: CallCollection },
        WAWebVoipStackInterface: { getVoipStackInterface: async () => stack },
        WAWebVoipStartCall: {
            startWAWebVoipCall: async (wid, video, fromUi, r, callId, opts) => {
                started.push({ wid: String(wid), video, fromUi, opts });
                CallCollection.activeCall = makeCall('OUT1', 1, { outgoing: true, peerJid: wid });
            },
        },
        WAWebWidFactory: { createWid: (s) => new Wid(s) },
        WAWebQueryExistsJob: {
            queryPhoneExists: async (p) => (p === '+491709999999' ? null : { wid: new Wid(p.slice(1) + '@c.us') }),
        },
        WAWebLidMigrationUtils: {
            toPn: (w) => (String(w) === '99887766@lid' ? new Wid('491701111001@c.us') : null),
            toLid: (w) => (String(w) === '491701111001@c.us' ? new Wid('99887766@lid') : null),
        },
        WAWebContactCollection: {
            ContactCollection: {
                get: (k) => contacts.find((c) => String(c.id) === String(k)) || null,
                getModelsArray: () => contacts,
            },
        },
        WAWebVoipSignalingEnums: { EndCallReason: { Self: 2 } },
        WAWebWamEnumCallFromUi: { CALL_FROM_UI: { CONVERSATION: 8 } },
        WAWebVoipAcquireMediaStream: { checkVoipDevicePermissions: async () => true },
        WAWebCallRingtone: { stopCallRingtone() {} },
        WAWebMuteCollection: {
            MuteCollection: {
                sounds: true,
                out: true,
                getGlobalSounds() {
                    return this.sounds;
                },
                setGlobalSounds(v) {
                    this.sounds = v;
                },
                getOutgoingMessageSound() {
                    return this.out;
                },
                setOutgoingMessageSound(v) {
                    this.out = v;
                },
            },
        },
        WAWebUserPrefsMeUser: {
            getMaybeMePnUser: () => new Wid('490000@c.us'),
            getMaybeMeLidUser: () => new Wid('1@lid'),
            getMaybeMeDisplayName: () => 'Me',
        },
    };
    globalThis.window = {
        require: (n) => {
            if (!(n in modules)) throw new Error('Requiring unknown module "' + n + '"');
            return modules[n];
        },
        __wa2sipEmit: (j) => emitted.push(JSON.parse(j)),
    };
    globalThis.document = {
        getElementById: () => null,
        createElement: () => ({ style: {}, setAttribute() {}, addEventListener() {} }),
        body: { appendChild() {} },
        visibilityState: 'visible',
    };
    assert.strictEqual(installPageApi(), 'installed');
    const api = globalThis.window.__wa2sip;
    return { api, emitted, started, stack, CallCollection, makeCall, modules };
}

test('call monitor reports new calls, state changes and removal', () => {
    const { api, emitted, CallCollection, makeCall } = setup();
    try {
        const call = makeCall('IN1', 3);
        CallCollection.models.push(call);
        CallCollection.activeCall = call;
        api.tick();
        assert.strictEqual(emitted.length, 1);
        const snap = emitted[0].call;
        assert.strictEqual(snap.id, 'IN1');
        assert.strictEqual(snap.state, 3);
        assert.deepStrictEqual(snap.peer, {
            jid: '491701111001@c.us',
            lid: '99887766@lid',
            pn_jid: '491701111001@c.us',
            number: '491701111001',
            name: 'Alice',
        });
        api.tick();
        assert.strictEqual(emitted.length, 1, 'no duplicate without a change');
        call._s = 6;
        api.tick();
        assert.strictEqual(emitted[1].call.state, 6);
        CallCollection.models = [];
        CallCollection.activeCall = null;
        api.tick();
        assert.strictEqual(emitted[2].call.removed, true);
    } finally {
        api.stop();
    }
});

test('peer given as a LID resolves its phone number', () => {
    const { api, emitted, CallCollection, makeCall } = setup();
    try {
        CallCollection.activeCall = makeCall('IN2', 3, { peerJid: new Wid('99887766@lid') });
        api.tick();
        const p = emitted[0].call.peer;
        assert.strictEqual(p.lid, '99887766@lid');
        assert.strictEqual(p.number, '491701111001');
    } finally {
        api.stop();
    }
});

test('dial resolves the number and starts a voice call like a user click', async () => {
    const { api, started } = setup();
    try {
        const snap = await api.api.dial('+49 170 1111002');
        assert.strictEqual(snap.id, 'OUT1');
        assert.strictEqual(snap.outgoing, true);
        assert.deepStrictEqual(started, [
            { wid: '491701111002@c.us', video: false, fromUi: 8, opts: { entryTrust: 'user_gesture' } },
        ]);
        await assert.rejects(api.api.dial('491701111003'), (e) => e.code === 'busy');
    } finally {
        api.stop();
    }
});

test('dial by WhatsApp id and unknown numbers', async () => {
    const { api, started } = setup();
    try {
        await api.api.dial('99887766@lid');
        assert.strictEqual(started[0].wid, '99887766@lid');
    } finally {
        api.stop();
    }
    const env2 = setup();
    try {
        await assert.rejects(env2.api.api.dial('491709999999'), (e) => e.code === 'not_on_whatsapp');
        await assert.rejects(env2.api.api.dial('12'), (e) => e.code === 'bad_target');
    } finally {
        env2.api.stop();
    }
});

test('accept, reject and hang up go through the VoIP stack', async () => {
    const { api, stack, CallCollection, makeCall } = setup();
    try {
        await assert.rejects(api.api.accept('X'), (e) => e.code === 'no_such_call');
        CallCollection.activeCall = makeCall('IN3', 3);
        await api.api.accept('IN3');
        assert.deepStrictEqual(stack.accepted, [[true, false]]);
        await api.api.hangup('IN3');
        assert.deepStrictEqual(stack.ended, [[2, true]]);
        await api.api.reject(null);
        assert.strictEqual(stack.rejected, 1);
    } finally {
        api.stop();
    }
});

test('contacts: people only, merged by phone number, with names', () => {
    const { api } = setup();
    try {
        const list = api.api.contacts();
        const byNumber = Object.fromEntries(list.map((c) => [c.number || c.id, c]));
        assert.strictEqual(list.length, 2);
        assert.strictEqual(byNumber['491701111001'].name, 'Alice');
        assert.strictEqual(byNumber['491701111001'].lid, '99887766@lid');
        assert.strictEqual(byNumber['555@lid'].pushname, 'Stranger');
        assert.deepStrictEqual(api.api.me(), { id: '490000@c.us', lid: '1@lid', number: '490000', name: 'Me' });
    } finally {
        api.stop();
    }
});

test('message sounds are turned off once', () => {
    const { api, modules } = setup();
    try {
        assert.strictEqual(api.api.quiet(), true);
        assert.strictEqual(modules.WAWebMuteCollection.MuteCollection.sounds, false);
        assert.strictEqual(modules.WAWebMuteCollection.MuteCollection.out, false);
        assert.strictEqual(api.api.quiet(), false);
    } finally {
        api.stop();
    }
});

test('gesture runner executes the armed action once', async () => {
    const { api, started } = setup();
    try {
        api.arm('dial', ['491701111002'], 't1');
        assert.deepStrictEqual(api.result('t1'), { done: false });
        api.runArmed('t1');
        api.runArmed('t1');
        for (let i = 0; i < 50 && !(api.result('t1') || {}).done; i++) await new Promise((r) => setTimeout(r, 10));
        assert.strictEqual(started.length, 1);
    } finally {
        api.stop();
    }
});

test('raw microphone turns off browser audio processing', async () => {
    let seen = null;
    Object.defineProperty(globalThis, 'navigator', {
        value: { mediaDevices: { getUserMedia: async (c) => (seen = c) } },
        configurable: true,
    });
    rawMicrophone();
    await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, deviceId: 'x' }, video: false });
    assert.deepStrictEqual(seen, {
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, deviceId: 'x' },
        video: false,
    });
});
