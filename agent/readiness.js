'use strict';
/**
 * When is the linked session really "ready", and when is a non-ready state a lie?
 *
 * whatsapp-web.js reports its progress through events ('loading_screen', 'authenticated', 'ready',
 * 'change_state', 'disconnected') whose order is not fixed. After a page reload — WhatsApp Web
 * ships a new build almost daily, and `webVersionCache: none` means we always get the new one — a
 * 'ready' can be followed by a late 'loading_screen'. Mirroring whatever came last then leaves the
 * engine convinced the account is *loading* while the page is connected, and every call is refused
 * with "WhatsApp is not connected" until somebody restarts the container (2026-10-23, twice).
 *
 * Kept separate from index.js so it can be unit-tested without launching Chromium.
 */

/** Should a 'loading_screen' event be forwarded to the engine? No once we are ready. */
function shouldReportLoading(ready) {
    return !ready;
}

/**
 * Is this a stall worth recovering from — a page that is connected while the engine is not ready?
 *
 * @param {{ready: boolean, state: string, socket: (string|null), shuttingDown: boolean}} s
 *   `socket` is WhatsApp Web's own connection state (`WAWebSocketModel.Socket.state` from the
 *   in-page diagnostics). 'qr' and 'failed' are left alone: they wait for a human.
 * @returns {boolean}
 */
function isStalled(s) {
    if (!s || s.ready || s.shuttingDown) return false;
    if (s.socket !== 'CONNECTED') return false;
    return s.state !== 'qr' && s.state !== 'failed';
}

/** Two checks in a row, ~60 s: one is not enough, a normal start-up can look stalled for a moment. */
const STALL_CHECKS = 2;

/**
 * Folds one health check into the stall counter.
 *
 * @param {number} count      the previous counter
 * @param {boolean} stalled  result of isStalled() for this check
 * @returns {{count: number, recover: boolean}} recover only after consecutive stalls
 */
function foldStall(count, stalled) {
    const next = stalled ? count + 1 : 0;
    return { count: next, recover: next >= STALL_CHECKS };
}

/**
 * Resolves like `promise`, or with `fallback` after `ms`. A crashed or frozen WhatsApp tab never
 * answers page.evaluate() (puppeteer waits for its protocolTimeout, minutes), so liveness probes
 * must not wait on it.
 */
function withTimeout(promise, ms, fallback) {
    let timer;
    const timeout = new Promise((resolve) => {
        timer = setTimeout(() => resolve(fallback), ms);
    });
    return Promise.race([Promise.resolve(promise).catch(() => fallback), timeout]).finally(() => clearTimeout(timer));
}

/**
 * Should a 'ready' session be torn down because its tab is dead?
 *
 * 'ready' only says WhatsApp Web loaded once. The renderer can be OOM-killed or freeze later
 * (2026-11-16: killed after 15 days at ~850 MB in a 2 GiB container): the bridge then kept saying
 * 'ready' for 7 hours, missed every incoming call and every dial hung. Two failed probes in a row
 * (~60 s) restart the agent; one can be a busy moment (a call starting, a big sync).
 *
 * @param {number} count  failed probes so far
 * @param {boolean} alive  result of this probe
 * @returns {{count: number, restart: boolean}}
 */
function foldDeadTab(count, alive) {
    const next = alive ? 0 : count + 1;
    return { count: next, restart: next >= DEAD_TAB_CHECKS };
}
const DEAD_TAB_CHECKS = 2;

module.exports = { shouldReportLoading, isStalled, foldStall, STALL_CHECKS, withTimeout, foldDeadTab, DEAD_TAB_CHECKS };
