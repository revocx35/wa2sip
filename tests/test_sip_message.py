from wa2sip.sip.auth import build_authorization, parse_challenge, pick_challenge
from wa2sip.sip.message import NameAddr, SipMessage, SipUri, Via, split_values

INVITE = (
    "INVITE sip:c2s-abc@192.168.1.23:5062 SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 192.168.1.10:5060;rport;branch=z9hG4bKPj1\r\n"
    "v: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK2, SIP/2.0/TCP 10.0.0.2;branch=z9hG4bK3\r\n"
    'f: "Front, Desk" <sip:1001@192.168.1.10>;tag=abc\r\n'
    "t: <sip:1008@192.168.1.10>\r\n"
    "i: call-1@host\r\n"
    "CSeq: 7 INVITE\r\n"
    "m: <sip:asterisk@192.168.1.10:5060>\r\n"
    "Subject: folded\r\n"
    " continuation\r\n"
    "c: application/sdp\r\n"
    "l: 4\r\n"
    "\r\n"
    "v=0\r\nEXTRA"
).encode()


def test_parse_request_with_compact_and_folded_headers():
    m = SipMessage.parse(INVITE)
    assert m.method == "INVITE" and m.uri == "sip:c2s-abc@192.168.1.23:5062"
    assert m.call_id == "call-1@host"
    assert m.cseq == (7, "INVITE")
    assert m.from_.tag == "abc" and m.from_.display == "Front, Desk"
    assert m.to.tag is None
    assert m.values("Via")[1].startswith("SIP/2.0/UDP 10.0.0.1")
    assert len(m.values("Via")) == 3
    assert m.branch == "z9hG4bKPj1"
    assert m.get("Subject") == "folded continuation"
    assert m.content_type == "application/sdp"
    assert m.body == b"v=0\r"  # Content-Length respected


def test_serialize_roundtrip_sets_content_length():
    m = SipMessage(status=200, reason="OK", headers=[("Call-ID", "x"), ("CSeq", "1 BYE")], body=b"hello")
    out = SipMessage.parse(m.to_bytes())
    assert out.status == 200 and out.reason == "OK"
    assert out.get("content-length") == "5" and out.body == b"hello"


def test_uri_nameaddr_via_helpers():
    u = SipUri.parse("sip:1008:pw@pbx.local:5080;transport=tcp;lr?Subject=x")
    assert (u.user, u.password, u.host, u.port, u.transport) == ("1008", "pw", "pbx.local", 5080, "tcp")
    assert "lr" in u.params and str(u).startswith("sip:1008:pw@pbx.local:5080;transport=tcp;lr")
    na = NameAddr.parse("sip:1001@h;tag=t1")
    assert na.uri == "sip:1001@h" and na.tag == "t1"
    v = Via.parse("SIP/2.0/UDP [::1]:5060;branch=z9hG4bKx;rport")
    assert v.host == "[::1]" and v.port == 5060 and v.branch == "z9hG4bKx" and "rport" in v.params
    assert split_values('"a,b" <sip:x>, <sip:y;p=1,2>') == ['"a,b" <sip:x>', "<sip:y;p=1,2>"]


def test_digest_rfc2617_example():
    ch = parse_challenge('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                         'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    hdr = build_authorization(ch, "GET", "/dir/index.html", "Mufasa", "Circle Of Life", nc=1, cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in hdr
    assert "qop=auth" in hdr and "nc=00000001" in hdr and 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in hdr


def test_digest_without_qop_and_challenge_selection():
    ch = pick_challenge(['Basic realm="x"', 'Digest realm="asterisk", nonce="abc", algorithm=MD5'])
    assert ch and ch["realm"] == "asterisk"
    hdr = build_authorization(ch, "REGISTER", "sip:pbx", "1008", "secret")
    assert "qop" not in hdr and 'uri="sip:pbx"' in hdr
    assert pick_challenge(['Digest realm="x", nonce="n", algorithm=AKAv1-MD5']) is None
