'use strict';
/**
 * wa2sip WhatsApp agent: one process per linked WhatsApp account.
 *
 * Runs WhatsApp Web in Chromium (whatsapp-web.js + puppeteer) and speaks JSON lines with the
 * Python engine on stdin/stdout (see wa2sip/wa/agent.py for the protocol). Chromium's audio goes
 * to the PulseAudio devices named in PULSE_SINK / PULSE_SOURCE, which the engine bridges to SIP.
 *
 * stdout is reserved for protocol messages: console output of libraries goes to stderr.
 */

const fs = require('fs');
const path = require('path');
const readline = require('readline');

for (const k of ['log', 'info', 'warn', 'error', 'debug']) {
    console[k] = (...args) => process.stderr.write(args.map(String).join(' ') + '\n');
}

const QRCode = require('qrcode');
const { Client, LocalAuth } = require('whatsapp-web.js');
const { installPageApi, rawMicrophone } = require('./page');

const env = process.env;
const ACCOUNT = env.WA2SIP_ACCOUNT || 'default';
const PROFILE_DIR = env.WA2SIP_PROFILE_DIR || path.join(__dirname, '.profile');
const CHROMIUM = env.WA2SIP_CHROMIUM || '/usr/bin/chromium';
const SANDBOX = env.WA2SIP_SANDBOX === '1';
const HEADLESS = env.WA2SIP_HEADLESS === '1';
const RAW_MIC = env.WA2SIP_RAW_MIC !== '0';
const TRACE = env.WA2SIP_TRACE === '1';
const READY_FALLBACK_MS = 90000;

function send(obj) {
    process.stdout.write(JSON.stringify(obj) + '\n');
}
function log(level, msg) {
    send({ event: 'log', level, msg: String(msg) });
}
function setState(state, detail) {
    send({ event: 'state', state, detail: detail || '' });
}

let client = null;
let page = null;
let ready = false;
let shuttingDown = false;
let readyFallback = null;

// -- browser -------------------------------------------------------------------------------
function chromiumArgs() {
    const args = [
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-dev-shm-usage',
        '--password-store=basic',
        '--use-mock-keychain',
        '--lang=en-US',
        '--window-size=1280,900',
        '--window-position=0,0',
        // calls must work without a human: no autoplay gate, no permission prompt for the microphone
        '--autoplay-policy=no-user-gesture-required',
        '--use-fake-ui-for-media-stream',
        // a background tab must keep its timers and audio running
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        '--disable-features=Translate,MediaRouter,DialMediaRouteProvider,OptimizationHints,CalculateNativeWinOcclusion,HardwareMediaKeyHandling,GlobalMediaControls',
        '--disable-sync',
        '--no-pings',
    ];
    if (!SANDBOX) args.push('--no-sandbox');
    if (HEADLESS) args.push('--headless=new');
    return args;
}

function removeStaleLocks() {
    // a crashed Chromium leaves its profile locked; only this agent ever uses the profile
    const dir = path.join(PROFILE_DIR, `session-${ACCOUNT}`);
    for (const f of ['SingletonLock', 'SingletonSocket', 'SingletonCookie']) {
        try {
            fs.rmSync(path.join(dir, f), { force: true });
        } catch (e) {
            /* not there */
        }
    }
}

async function onPage(p) {
    page = p;
    page.on('pageerror', (e) => TRACE && log('debug', 'page error: ' + e.message));
    page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame()) {
            ready = false;
            setTimeout(() => ensurePageApi().catch(() => {}), 3000);
        }
    });
    try {
        await page.exposeFunction('__wa2sipEmit', (json) => onPageEvent(json));
    } catch (e) {
        /* already exposed */
    }
    try {
        const ctx = page.browserContext();
        await ctx.overridePermissions('https://web.whatsapp.com', ['microphone']);
    } catch (e) {
        log('warn', 'could not grant the microphone permission: ' + e.message);
    }
}

async function ensurePageApi() {
    if (!page || page.isClosed()) return false;
    const has = await page.evaluate(() => !!(window.__wa2sip && window.__wa2sip.version === 1)).catch(() => false);
    if (has) return true;
    const ok = await page
        .evaluate(() => typeof window.require === 'function' && !!window.require('WAWebCallCollection'))
        .catch(() => false);
    if (!ok) return false;
    await page.evaluate(installPageApi);
    log('info', 'call monitor installed');
    return true;
}

function onPageEvent(json) {
    let msg;
    try {
        msg = JSON.parse(json);
    } catch (e) {
        return;
    }
    if (msg.type === 'call') {
        if (TRACE) log('debug', 'call snapshot ' + JSON.stringify(msg.call));
        send({ event: 'call', call: msg.call });
    }
}

async function pageCall(fn, ...args) {
    if (!(await ensurePageApi())) {
        const e = new Error('WhatsApp Web is not loaded');
        e.code = 'not_ready';
        throw e;
    }
    const r = await page.evaluate(
        async (fn, args) => {
            try {
                return { ok: true, value: await window.__wa2sip.api[fn](...args) };
            } catch (e) {
                return { ok: false, error: String((e && e.message) || e), code: (e && e.code) || 'error' };
            }
        },
        fn,
        args,
    );
    if (!r.ok) {
        const e = new Error(r.error);
        e.code = r.code;
        throw e;
    }
    return r.value;
}

/** Run a page action inside a real (trusted) mouse click, so it carries user activation. */
async function gestureCall(fn, ...args) {
    if (!(await ensurePageApi())) {
        const e = new Error('WhatsApp Web is not loaded');
        e.code = 'not_ready';
        throw e;
    }
    const token = Math.random().toString(36).slice(2);
    await page.evaluate((fn, args, token) => window.__wa2sip.arm(fn, args, token), fn, args, token);
    try {
        await page.click('#__wa2sip_gesture', { delay: 20 });
    } catch (e) {
        log('debug', 'gesture click failed (' + e.message + '), running without it');
        await page.evaluate((token) => window.__wa2sip.runArmed(token), token);
    }
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
        const r = await page.evaluate((token) => window.__wa2sip.result(token), token);
        if (r && r.done) {
            if (r.error) {
                const e = new Error(r.error);
                e.code = r.code;
                throw e;
            }
            return r.value;
        }
        await new Promise((res) => setTimeout(res, 100));
    }
    const e = new Error(fn + ' timed out');
    e.code = 'timeout';
    throw e;
}

// -- WhatsApp client -------------------------------------------------------------------------
async function onReady() {
    if (readyFallback) {
        clearTimeout(readyFallback);
        readyFallback = null;
    }
    for (let i = 0; i < 30 && !(await ensurePageApi()); i++) {
        await new Promise((r) => setTimeout(r, 1000));
    }
    try {
        if (await pageCall('quiet')) log('info', 'turned off WhatsApp Web message sounds (they would play into calls)');
    } catch (e) {
        log('debug', 'could not turn off message sounds: ' + e.message);
    }
    try {
        send({ event: 'me', me: await pageCall('me') });
    } catch (e) {
        log('warn', 'could not read own account: ' + e.message);
    }
    try {
        const diag = await pageCall('diag');
        send({ event: 'diag', diag });
        if (diag.enable_web_calling === false) {
            log('warn', 'WhatsApp has not enabled calling for this WhatsApp Web session (enable_web_calling=false)');
        }
    } catch (e) {
        log('debug', 'diagnostics failed: ' + e.message);
    }
    ready = true;
    setState('ready');
}

async function start() {
    fs.mkdirSync(PROFILE_DIR, { recursive: true, mode: 0o700 });
    removeStaleLocks();
    setState('loading', 'starting browser');
    client = new Client({
        authStrategy: new LocalAuth({ clientId: ACCOUNT, dataPath: PROFILE_DIR }),
        puppeteer: {
            executablePath: CHROMIUM,
            headless: HEADLESS ? 'new' : false,
            args: chromiumArgs(),
            defaultViewport: null,
            ignoreDefaultArgs: ['--mute-audio', '--enable-automation'],
            env: process.env,
            protocolTimeout: 120000,
        },
        userAgent: false,
        webVersionCache: { type: 'none' },
        takeoverOnConflict: true,
        takeoverTimeoutMs: 5000,
        authTimeoutMs: 120000,
        qrMaxRetries: 0,
        evalOnNewDoc: RAW_MIC ? rawMicrophone : undefined,
    });

    client.on('qr', async (qr) => {
        try {
            const url = await QRCode.toDataURL(qr, { margin: 1, width: 320, errorCorrectionLevel: 'L' });
            send({ event: 'qr', qr: url });
        } catch (e) {
            log('error', 'QR rendering failed: ' + e.message);
        }
    });
    client.on('code', (code) => send({ event: 'pairing_code', code }));
    client.on('loading_screen', (percent) => setState('loading', `syncing ${percent}%`));
    client.on('authenticated', () => {
        setState('authenticated', 'loading chats');
        if (!readyFallback) {
            readyFallback = setTimeout(async () => {
                readyFallback = null;
                if (!ready) {
                    log('info', 'no ready event from whatsapp-web.js - continuing without it');
                    await onReady();
                }
            }, READY_FALLBACK_MS);
        }
    });
    client.on('auth_failure', (msg) => setState('failed', 'authentication failed: ' + msg));
    client.on('ready', () => onReady().catch((e) => log('error', 'ready handling failed: ' + e.message)));
    client.on('change_state', (s) => {
        log('info', 'WhatsApp connection: ' + s);
        if (!ready) return;
        if (s === 'CONNECTED') setState('ready');
        else setState('disconnected', s);
    });
    client.on('disconnected', (reason) => {
        ready = false;
        log('warn', 'WhatsApp disconnected: ' + reason);
        setState(reason === 'LOGOUT' ? 'loading' : 'disconnected', String(reason));
    });

    const init = client.initialize();
    init.catch(() => {});
    // initialize() only returns after WhatsApp Web has loaded; hook the page as soon as it exists
    for (let i = 0; i < 600 && !client.pupPage; i++) await new Promise((r) => setTimeout(r, 50));
    if (client.pupBrowser) {
        client.pupBrowser.on('disconnected', () => {
            if (!shuttingDown) {
                log('error', 'browser closed');
                process.exit(3);
            }
        });
    }
    if (client.pupPage) await onPage(client.pupPage);
    await init;
}

// -- commands ---------------------------------------------------------------------------------
const commands = {
    async contacts() {
        return pageCall('contacts');
    },
    async lookup({ number }) {
        return pageCall('lookup', number);
    },
    async dial({ to }) {
        return gestureCall('dial', String(to));
    },
    async accept({ callId }) {
        return gestureCall('accept', callId || null);
    },
    async reject({ callId }) {
        return pageCall('reject', callId || null);
    },
    async hangup({ callId }) {
        return pageCall('hangup', callId || null);
    },
    async diagnostics() {
        const d = await pageCall('diag');
        send({ event: 'diag', diag: d });
        return d;
    },
    async screenshot() {
        if (!page || page.isClosed()) throw Object.assign(new Error('no browser page'), { code: 'not_running' });
        return page.screenshot({ type: 'jpeg', quality: 70, encoding: 'base64' });
    },
    async pairing_code({ phone }) {
        if (!client) throw Object.assign(new Error('not started'), { code: 'not_running' });
        return client.requestPairingCode(String(phone), true);
    },
    async logout() {
        ready = false;
        // client.logout() would close the browser; log out the way the WhatsApp Web menu does
        await page.evaluate(() => window.require('WAWebSocketModel').Socket.logout());
        return true;
    },
    async shutdown() {
        shuttingDown = true;
        setTimeout(async () => {
            try {
                if (client) await client.destroy();
            } catch (e) {
                /* already gone */
            }
            process.exit(0);
        }, 10);
        return true;
    },
};

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
    let msg;
    try {
        msg = JSON.parse(line);
    } catch (e) {
        return;
    }
    const fn = commands[msg.cmd];
    if (!fn) {
        send({ id: msg.id, ok: false, code: 'unknown_command', error: 'unknown command ' + msg.cmd });
        return;
    }
    try {
        const result = await fn(msg.args || {});
        send({ id: msg.id, ok: true, result: result === undefined ? null : result });
    } catch (e) {
        send({ id: msg.id, ok: false, code: e.code || 'error', error: String((e && e.message) || e) });
    }
});
rl.on('close', () => {
    // the engine went away: don't leave an orphaned browser behind
    shuttingDown = true;
    Promise.resolve(client && client.destroy())
        .catch(() => {})
        .finally(() => process.exit(0));
});

process.on('unhandledRejection', (e) => log('debug', 'unhandled: ' + ((e && e.stack) || e)));

start().catch((e) => {
    log('error', 'WhatsApp Web failed to start: ' + ((e && e.message) || e));
    setState('failed', String((e && e.message) || e));
    process.exit(2);
});
