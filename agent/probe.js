'use strict';
/**
 * WhatsApp Web compatibility probe: do the internal modules/functions wa2sip uses still exist?
 *
 * Loads web.whatsapp.com logged out (no account needed; the VoIP modules are defined anyway),
 * records every module definition (__d) and checks REQUIRED below. Run inside the image:
 *
 *   docker run --rm --entrypoint node ghcr.io/revocx35/wa2sip agent/probe.js
 *   ... agent/probe.js --source WAWebVoipStartCall     # print one module's source (for re-reverse-engineering)
 *   ... agent/probe.js --grep Voip                     # list module names matching a pattern
 *
 * Exit code 0 = everything found, 1 = something is missing (see docs/whatsapp-web-internals.md).
 */
const puppeteer = require('puppeteer');

const REQUIRED = {
    WAWebCallCollection: ['default.setActiveCall|setActiveCall', 'default.getModelsArray|getModelsArray'],
    WAWebVoipStartCall: ['startWAWebVoipCall'],
    WAWebVoipStackInterface: ['getVoipStackInterface'],
    WAWebVoipAcquireMediaStream: ['checkVoipDevicePermissions'],
    WAWebVoipSignalingEnums: ['EndCallReason'],
    WAWebVoipWaCallEnums: ['CallState'],
    WAWebWamEnumCallFromUi: ['CALL_FROM_UI'],
    WAWebCallRingtone: ['stopCallRingtone'],
    WAWebWidFactory: ['createWid'],
    WAWebQueryExistsJob: ['queryPhoneExists'],
    WAWebLidMigrationUtils: ['toPn', 'toLid'],
    WAWebContactCollection: ['ContactCollection'],
    WAWebUserPrefsMeUser: ['getMaybeMePnUser', 'getMaybeMeLidUser'],
    WAWebMuteCollection: ['MuteCollection'],
    WAWebSocketModel: ['Socket'],
    WAWebABProps: ['getABPropConfigValue'],
};

function args() {
    const a = process.argv.slice(2);
    const get = (k) => (a.includes(k) ? a[a.indexOf(k) + 1] : null);
    return { source: get('--source'), grep: get('--grep'), wait: Number(get('--wait') || 25) };
}

(async () => {
    const opt = args();
    const flags = ['--no-first-run', '--disable-dev-shm-usage', '--headless=new'];
    if (process.env.WA2SIP_SANDBOX !== '1') flags.push('--no-sandbox');
    const browser = await puppeteer.launch({
        executablePath: process.env.WA2SIP_CHROMIUM_PATH || process.env.WA2SIP_CHROMIUM || '/usr/bin/chromium',
        headless: 'new',
        args: flags,
    });
    try {
        const page = (await browser.pages())[0];
        const ua = (await browser.userAgent()).replace('HeadlessChrome', 'Chrome');
        await page.setUserAgent(ua);
        await page.evaluateOnNewDocument(() => {
            window.__probeMods = {};
            let real;
            Object.defineProperty(window, '__d', {
                configurable: true,
                get() {
                    return real;
                },
                set(v) {
                    real = function (name, deps, factory) {
                        try {
                            window.__probeMods[name] = factory;
                        } catch (e) {
                            /* ignore */
                        }
                        return v.apply(this, arguments);
                    };
                },
            });
        });
        await page.goto('https://web.whatsapp.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
        await new Promise((r) => setTimeout(r, opt.wait * 1000));
        const version = await page.evaluate(() => (window.Debug && window.Debug.VERSION) || null);
        if (opt.source) {
            const src = await page.evaluate((n) => (window.__probeMods[n] ? String(window.__probeMods[n]) : null), opt.source);
            process.stdout.write(src ? src + '\n' : `module ${opt.source} not found\n`);
            process.exitCode = src ? 0 : 1;
            return;
        }
        if (opt.grep) {
            const names = await page.evaluate((g) => Object.keys(window.__probeMods).filter((n) => new RegExp(g, 'i').test(n)), opt.grep);
            process.stdout.write(names.sort().join('\n') + '\n');
            return;
        }
        const report = await page.evaluate((required) => {
            const out = {};
            for (const [mod, keys] of Object.entries(required)) {
                let m = null;
                try {
                    m = window.require(mod);
                } catch (e) {
                    out[mod] = { found: false, error: String(e.message || e).slice(0, 120) };
                    continue;
                }
                const missing = [];
                for (const k of keys) {
                    const ok = k.split('|').some((alt) => {
                        let v = m;
                        for (const part of alt.split('.')) v = v == null ? v : v[part];
                        return v !== undefined;
                    });
                    if (!ok) missing.push(k);
                }
                out[mod] = { found: true, missing };
            }
            return out;
        }, REQUIRED);
        const env = await page.evaluate(() => ({
            sharedArrayBuffer: typeof SharedArrayBuffer !== 'undefined',
            crossOriginIsolated: !!window.crossOriginIsolated,
            rtcPeerConnection: typeof RTCPeerConnection !== 'undefined',
            modules: Object.keys(window.__probeMods).length,
        }));
        const bad = Object.entries(report).filter(([, r]) => !r.found || r.missing.length);
        process.stdout.write(JSON.stringify({ waVersion: version, browser: env, modules: report }, null, 2) + '\n');
        process.stdout.write(bad.length ? `MISSING: ${bad.map(([m]) => m).join(', ')}\n` : 'OK: all WhatsApp Web internals wa2sip uses are present\n');
        process.exitCode = bad.length ? 1 : 0;
    } finally {
        await browser.close();
    }
})().catch((e) => {
    process.stderr.write(String((e && e.stack) || e) + '\n');
    process.exit(2);
});
