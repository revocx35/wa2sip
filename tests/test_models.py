import pytest

from wa2sip.models import Bridge, BridgeContact, Config, WaAccount
from wa2sip.sessions import normalize_number
from wa2sip.store import Store
from wa2sip.wa.agent import call_from_snapshot


def contact(n, **kw):
    return BridgeContact(number=n, name=f"C{n[-2:]}", **kw)


def bridge(**kw):
    kw.setdefault("wa_account_id", "wa1")
    kw.setdefault("extension_id", "e1")
    return Bridge(**kw)


def test_menu_codes_single_digits_skip_dial_digit_and_explicit_keys():
    b = bridge(contacts=[contact("4911"), contact("4912", digit="2"), contact("4913")],
               dial_number=True, dial_digit="1")
    assert [(code, c.number) for code, c in b.menu_codes()] == [("3", "4911"), ("2", "4912"), ("4", "4913")]


def test_menu_codes_two_digits_for_big_menus():
    b = bridge(contacts=[contact(f"49{i:02d}") for i in range(12)])
    codes = [code for code, _ in b.menu_codes()]
    assert codes[:3] == ["10", "11", "12"] and len(set(codes)) == 12
    assert all(len(c) == 2 for c in codes)


def test_menu_codes_skip_contacts_not_in_menu():
    b = bridge(contacts=[contact("4911", in_menu=False), contact("4912")])
    assert [(code, c.number) for code, c in b.menu_codes()] == [("1", "4912")]


def test_contact_validation():
    with pytest.raises(ValueError):
        BridgeContact(name="nobody")
    c = BridgeContact(number="+49 (151) 123-45", name="")
    assert c.number == "4915112345" and c.label() == "+4915112345" and c.target() == "4915112345"
    assert BridgeContact(wa_id="1234@lid").target() == "1234@lid"
    with pytest.raises(ValueError):
        BridgeContact(number="1", digit="1a")


def store_with(tmp_path, *bridges):
    s = Store(tmp_path)
    s.config = Config(wa_accounts=[WaAccount(id="wa1"), WaAccount(id="wa2")], bridges=list(bridges))
    return s


def test_routing_prefers_listed_contact_then_catch_all(tmp_path):
    listed = bridge(id="b1", extension_id="e1", contacts=[BridgeContact(wa_id="999@lid", number="4911", name="A")],
                    ring_targets=["2001"])
    catch = bridge(id="b2", extension_id="e2", all_contacts=True, ring_targets=["2002"])
    s = store_with(tmp_path, listed, catch)
    b, c, why = s.route_wa_call("wa1", {"jid": "999@lid"})
    assert b.id == "b1" and c.name == "A"
    b, c, _ = s.route_wa_call("wa1", {"jid": "777@lid", "number": "4911"})
    assert b.id == "b1"                                # matched by phone number
    b, c, why = s.route_wa_call("wa1", {"jid": "4955@c.us", "number": "4955"})
    assert b.id == "b2" and c is None and why == "catch-all bridge"
    assert s.route_wa_call("wa2", {"jid": "4955@c.us"})[0] is None
    listed.inbound_enabled = False
    assert s.route_wa_call("wa1", {"jid": "999@lid"})[0].id == "b2"


def test_routing_conflicts(tmp_path):
    a = bridge(id="b1", extension_id="e1", contacts=[contact("4911")])
    s = store_with(tmp_path, a)
    assert "already rings through" in s.routing_conflict(bridge(id="b2", extension_id="e2",
                                                                contacts=[contact("4911")]))
    assert s.routing_conflict(bridge(id="b2", extension_id="e2", contacts=[contact("4911", inbound=False)])) is None
    assert s.routing_conflict(bridge(id="b2", extension_id="e2", wa_account_id="wa2",
                                     contacts=[contact("4911")])) is None
    assert "extension is already used" in s.routing_conflict(bridge(id="b2", extension_id="e1"))
    s.config.bridges.append(bridge(id="b3", extension_id="e3", all_contacts=True))
    assert "catch-all" in s.routing_conflict(bridge(id="b4", extension_id="e4", all_contacts=True))
    dup = bridge(id="b5", extension_id="e5", wa_account_id="wa2",
                 contacts=[contact("4921", digit="1"), contact("4922", digit="1")])
    assert "used twice" in s.routing_conflict(dup)


def test_normalize_number():
    assert normalize_number("00491511234", "0", "90") == "491511234"
    assert normalize_number("05321234567", "0", "90") == "905321234567"
    assert normalize_number("05321234567", "0", "") == "05321234567"
    assert normalize_number("491511234", "0", "90") == "491511234"


@pytest.mark.parametrize("snap,state,reason", [
    ({"id": "A", "state": 3}, "incoming", ""),
    ({"id": "A", "state": 1, "outgoing": True}, "calling", ""),
    ({"id": "A", "state": 2, "outgoing": True}, "ringing", ""),
    ({"id": "A", "state": 6}, "active", ""),
    ({"id": "A", "state": 7}, "elsewhere", "answered on another device"),
    ({"id": "A", "state": 0, "everConnected": True}, "ended", "hung up"),
    ({"id": "A", "state": 0, "peerBusy": True, "outgoing": True}, "ended", "busy"),
    ({"id": "A", "state": 0, "logResult": 3, "outgoing": True}, "ended", "declined"),
    ({"id": "A", "state": 0, "outgoing": True}, "ended", "not answered"),
    ({"id": "A", "state": 6, "removed": True, "everConnected": True}, "ended", "hung up"),
])
def test_call_snapshot_mapping(snap, state, reason):
    call = call_from_snapshot("wa1", snap)
    assert call.state == state and call.reason == reason
