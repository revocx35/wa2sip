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

module.exports = { shouldReportLoading, isStalled, foldStall, STALL_CHECKS };
