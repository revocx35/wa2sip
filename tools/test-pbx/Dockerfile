# Minimal Asterisk for wa2sip end-to-end tests (UDP 5160, extensions 1009/1010 for wa2sip, 2001/2002 test phones)
FROM alpine:3.22
RUN apk add --no-cache asterisk asterisk-sounds-en && rm -f /etc/asterisk/*.conf
COPY conf/ /etc/asterisk/
CMD ["asterisk", "-f", "-U", "asterisk", "-G", "asterisk", "-vvv"]
