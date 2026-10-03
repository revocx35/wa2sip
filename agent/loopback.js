'use strict';
/**
 * Audio plumbing self-test: a Chromium page that plays its microphone back to its speaker.
 * Used by tools/audio_loopback.py to check Chromium <-> PulseAudio without WhatsApp.
 * Same Chromium flags and microphone handling as the real agent.
 */
const http = require('http');
const puppeteer = require('puppeteer');
const { rawMicrophone } = require('./page');

(async () => {
    const args = [
        '--no-first-run',
        '--autoplay-policy=no-user-gesture-required',
        '--use-fake-ui-for-media-stream',
        '--disable-dev-shm-usage',
    ];
    if (process.env.WA2SIP_SANDBOX !== '1') args.push('--no-sandbox');
    if (process.env.WA2SIP_HEADLESS === '1') args.push('--headless=new');
    const browser = await puppeteer.launch({
        executablePath: process.env.WA2SIP_CHROMIUM || '/usr/bin/chromium',
        headless: process.env.WA2SIP_HEADLESS === '1' ? 'new' : false,
        args,
        ignoreDefaultArgs: ['--mute-audio'],
        env: process.env,
    });
    const page = (await browser.pages())[0];
    await page.evaluateOnNewDocument(rawMicrophone);
    // getUserMedia needs a secure context: http://127.0.0.1 counts as one
    const server = http.createServer((req, res) => res.end('<!doctype html><title>loopback</title>'));
    await new Promise((r) => server.listen(0, '127.0.0.1', r));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    const info = await page.evaluate(async () => {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
        const ctx = new AudioContext();
        await ctx.resume();
        ctx.createMediaStreamSource(stream).connect(ctx.destination);
        const t = stream.getAudioTracks()[0];
        window.__keep = { stream, ctx };
        return { label: t.label, settings: t.getSettings(), state: ctx.state, rate: ctx.sampleRate };
    });
    process.stdout.write(JSON.stringify({ ready: true, info }) + '\n');
    process.stdin.on('data', () => {});
    process.stdin.on('end', async () => {
        await browser.close();
        process.exit(0);
    });
})().catch((e) => {
    process.stdout.write(JSON.stringify({ error: String(e && e.stack || e) }) + '\n');
    process.exit(1);
});
