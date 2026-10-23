'use strict';
/**
 * The session state machine, tested without Chromium: a late 'loading_screen' must not pull a
 * ready session back to 'loading', and a page that is connected while the engine is stuck must be
 * recoverable. Both were one bug on 2026-10-23: the bridge answered "WhatsApp is not connected" to
 * every call while WhatsApp Web was up and logged in.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { shouldReportLoading, isStalled, foldStall } = require('../readiness');

test('a late loading_screen does not pull a ready session back to loading', () => {
    assert.equal(shouldReportLoading(true), false);
    assert.equal(shouldReportLoading(false), true);
});

test('isStalled: a connected page that never reported ready is a stall', () => {
    assert.equal(isStalled({ ready: false, state: 'loading', socket: 'CONNECTED', shuttingDown: false }), true);
    assert.equal(isStalled({ ready: false, state: 'disconnected', socket: 'CONNECTED', shuttingDown: false }), true);
});

test('isStalled: genuine states are left alone', () => {
    // not connected: a real disconnection, the client reconnects on its own
    assert.equal(isStalled({ ready: false, state: 'loading', socket: 'TIMEOUT', shuttingDown: false }), false);
    assert.equal(isStalled({ ready: false, state: 'loading', socket: null, shuttingDown: false }), false);
    // waiting for a human
    assert.equal(isStalled({ ready: false, state: 'qr', socket: 'CONNECTED', shuttingDown: false }), false);
    assert.equal(isStalled({ ready: false, state: 'failed', socket: 'CONNECTED', shuttingDown: false }), false);
    // already ready, or shutting down
    assert.equal(isStalled({ ready: true, state: 'loading', socket: 'CONNECTED', shuttingDown: false }), false);
    assert.equal(isStalled({ ready: false, state: 'loading', socket: 'CONNECTED', shuttingDown: true }), false);
    assert.equal(isStalled(null), false);
});

test('foldStall: one stalled check is not a stall, two in a row are', () => {
    let s = foldStall(0, true);
    assert.deepEqual(s, { count: 1, recover: false });
    s = foldStall(s.count, true);
    assert.deepEqual(s, { count: 2, recover: true });
    // a normal start-up looks stalled for a moment and must not be "recovered"
    s = foldStall(0, true);
    s = foldStall(s.count, false);
    assert.deepEqual(s, { count: 0, recover: false });
    assert.deepEqual(foldStall(0, false), { count: 0, recover: false });
});

test('index.js wires the guard and the recovery check (a unit test cannot see this)', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'index.js'), 'utf8');
    assert.match(src, /shouldReportLoading\(ready\)/, 'the loading_screen handler must ask readiness.js');
    assert.match(src, /isStalled\(/, 'the health check must use readiness.js');
    assert.match(src, /foldStall\(stalls, isStalled\(/, 'a recovery must require two consecutive stalls');
    assert.match(src, /setInterval\(\(\) => \{ void checkHealth\(\); \}/, 'the health check must be scheduled');
});
