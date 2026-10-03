# wa2sip: WhatsApp (linked device, real WhatsApp Web in Chromium) <-> SIP PBX bridge.
# One image: Python engine (SIP, IVR, web UI), Node agent (whatsapp-web.js), Chromium,
# Xvfb, PulseAudio, Piper TTS.

ARG NODE_IMAGE=node:22-trixie-slim

# ------------------------------------------------------------------ WhatsApp agent dependencies
FROM ${NODE_IMAGE} AS agent
WORKDIR /agent
ENV PUPPETEER_SKIP_DOWNLOAD=true
COPY agent/package.json agent/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY agent/*.js ./
RUN node --check index.js && node --check page.js

# ------------------------------------------------------------------ runtime
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WA2SIP_DATA_DIR=/data \
    WA2SIP_AGENT_DIR=/app/agent \
    WA2SIP_CHROMIUM_PATH=/usr/bin/chromium \
    PUPPETEER_SKIP_DOWNLOAD=true

# chromium: WhatsApp Web; xvfb: its private display; pulseaudio(-utils): virtual audio devices;
# espeak-ng: fallback TTS; util-linux: unshare (sandbox probe); fonts for WhatsApp Web's UI
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       chromium xvfb pulseaudio pulseaudio-utils espeak-ng util-linux tini ca-certificates \
       fonts-noto-core fonts-noto-color-emoji fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY --from=agent /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=agent /agent /app/agent
COPY wa2sip ./wa2sip
COPY tools/audio_loopback.py ./tools/audio_loopback.py

RUN useradd --system --uid 1000 --create-home --home-dir /home/wa2sip wa2sip \
    && mkdir -p /data && chown wa2sip:wa2sip /data && chmod 0700 /data
USER wa2sip
ENV HOME=/home/wa2sip
VOLUME /data

LABEL org.opencontainers.image.source="https://github.com/revocx35/wa2sip" \
      org.opencontainers.image.description="Bridge WhatsApp calls (linked device) to SIP PBX extensions, with IVR and natural voices" \
      org.opencontainers.image.licenses="MIT"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('WA2SIP_WEB_PORT', '8092'), timeout=4)"

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "wa2sip"]
