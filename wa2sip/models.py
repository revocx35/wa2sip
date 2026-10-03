"""Persistent configuration models (stored as JSON in the data directory)."""

from __future__ import annotations

import re
import secrets
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SECRET_FIELDS = {"password"}
DTMF_KEYS = "0123456789*#"
DEFAULT_VOICE = "piper:en_US-lessac-medium"


def new_id() -> str:
    return secrets.token_hex(4)


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _required(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("required")
    return v


# -- PBX side ------------------------------------------------------------------------------
class Pbx(BaseModel):
    """A SIP server (PBX) that wa2sip registers extensions on."""
    id: str = Field(default_factory=new_id)
    name: str = ""
    host: str                        # hostname or IP of the PBX
    port: int = Field(default=5060, ge=1, le=65535)
    domain: str = ""                 # SIP domain / realm, defaults to host
    expires: int = Field(default=300, ge=60, le=3600)
    enabled: bool = True

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return _required(v)


class Extension(BaseModel):
    """A SIP account on a PBX, registered by wa2sip (the number you call to reach WhatsApp)."""
    id: str = Field(default_factory=new_id)
    pbx_id: str
    username: str                    # the extension number, e.g. 1009
    password: str = ""
    auth_username: str = ""          # defaults to username
    display_name: str = ""
    enabled: bool = True
    # unique user part of our Contact: routes inbound INVITEs when several extensions share the port
    contact_user: str = Field(default_factory=lambda: "wa-" + secrets.token_hex(4))

    @field_validator("username", "pbx_id")
    @classmethod
    def _req(cls, v: str) -> str:
        return _required(v)


# -- WhatsApp side --------------------------------------------------------------------------
class WaAccount(BaseModel):
    """A WhatsApp account linked as a device (one WhatsApp Web instance)."""
    id: str = Field(default_factory=new_id)
    name: str = "WhatsApp"
    enabled: bool = True
    # incoming WhatsApp calls that no bridge routes: let them ring on the phone, or decline them
    unrouted: Literal["ignore", "reject"] = "ignore"


# -- bridges ----------------------------------------------------------------------------------
class BridgeContact(BaseModel):
    """A WhatsApp contact handled by a bridge (inbound routing and/or IVR menu entry)."""
    wa_id: str = ""                  # WhatsApp id (…@c.us or …@lid) when picked from the contact list
    number: str = ""                 # phone number with country code (digits), for dialing and matching
    name: str = ""                   # spoken in the menu and shown as caller name
    digit: str = ""                  # IVR key(s); assigned automatically when empty
    ring: list[str] = []             # extensions to ring for this contact (default: the bridge's)
    inbound: bool = True             # route this contact's WhatsApp calls through this bridge
    in_menu: bool = True             # offer this contact in the IVR menu

    @field_validator("number")
    @classmethod
    def _number(cls, v: str) -> str:
        return digits_only(v)

    @field_validator("digit")
    @classmethod
    def _digit(cls, v: str) -> str:
        v = (v or "").strip()
        if v and (len(v) > 3 or any(c not in "0123456789" for c in v)):
            raise ValueError("menu keys are 1-3 digits")
        return v

    @field_validator("ring")
    @classmethod
    def _ring(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x and x.strip()]

    @model_validator(mode="after")
    def _identity(self) -> "BridgeContact":
        if not self.wa_id and not self.number:
            raise ValueError("a contact needs a WhatsApp id or a phone number")
        return self

    def label(self) -> str:
        return self.name.strip() or (f"+{self.number}" if self.number else self.wa_id.split("@")[0])

    def target(self) -> str:
        """What to dial on WhatsApp: the id when known, else the number."""
        return self.wa_id or self.number


class Bridge(BaseModel):
    """Connects a WhatsApp account (all of it, or some contacts) with a PBX extension."""
    id: str = Field(default_factory=new_id)
    name: str = ""
    enabled: bool = True
    wa_account_id: str
    extension_id: str
    all_contacts: bool = False       # take every WhatsApp caller that no other bridge lists
    contacts: list[BridgeContact] = []

    # WhatsApp -> PBX: an incoming WhatsApp call rings these extensions (first to answer wins)
    inbound_enabled: bool = True
    ring_targets: list[str] = []
    ring_timeout: int = Field(default=30, ge=5, le=300)
    caller_name: str = "WA {name}"   # display name presented to the PBX
    # caller number presented (P-Asserted-Identity): the WhatsApp number, or the extension itself
    caller_number: Literal["whatsapp", "extension"] = "whatsapp"
    announce: bool = True            # tell the answering phone who is calling before connecting
    announce_text: str = "WhatsApp call from {name}."
    reject_unanswered: bool = True   # decline the WhatsApp call when no extension answers

    # PBX -> WhatsApp: calling the extension plays an IVR menu of the bridge's contacts
    outbound_enabled: bool = True
    allowed_callers: list[str] = []  # PBX callers allowed to use the bridge (empty = everyone)
    menu_always: bool = False        # also play the menu when there is only one contact
    dial_number: bool = False        # menu option: dial any phone number
    dial_digit: str = "0"
    national_prefix: str = "0"       # dial-a-number: a leading national prefix ...
    country_code: str = ""           # ... is replaced by this country code (e.g. 90)
    ringback: bool = True            # ring tone for the PBX caller while WhatsApp rings
    dial_timeout: int = Field(default=60, ge=10, le=180)
    after_call: Literal["hangup", "menu"] = "hangup"
    menu_digit: str = "*"            # during a WhatsApp call: hang it up and go back to the menu
    max_call_seconds: int = Field(default=4 * 3600, ge=60, le=24 * 3600)
    ivr_greeting: str = "Welcome."
    ivr_option_text: str = "Press {digit} for {name}."
    ivr_dial_text: str = "Press {digit} to dial a phone number."
    ivr_enter_text: str = "Enter the phone number with the country code, then press the hash key."
    ivr_invalid_text: str = "Sorry, that is not a valid choice."
    ivr_calling_text: str = "Calling {name}."
    ivr_failed_text: str = "{name} is not available right now."
    ivr_busy_text: str = "{name} is busy."
    ivr_not_on_wa_text: str = "This number is not on WhatsApp."
    ivr_wa_busy_text: str = "WhatsApp is already in another call. Please try again later."
    ivr_offline_text: str = "WhatsApp is not connected right now."
    ivr_goodbye_text: str = "Goodbye."
    ivr_repeats: int = Field(default=3, ge=1, le=10)
    ivr_timeout: int = Field(default=6, ge=2, le=30)    # seconds to wait for a key after the menu

    # PIN: asked before the menu (PBX -> WhatsApp) and/or before a WhatsApp call is answered
    pin: str = ""                    # 4-16 digits; empty = no PIN
    pin_outbound: bool = True        # callers of the extension must enter it
    pin_inbound: bool = True         # whoever picks up an incoming WhatsApp call must enter it
    pin_attempts: int = Field(default=3, ge=1, le=10)
    pin_prompt_text: str = "Please enter your PIN, then press the hash key."
    pin_wrong_text: str = "Wrong PIN."

    voice: str = ""                  # "" = the default voice (Settings)
    speed: int = Field(default=0, ge=0, le=400)          # words per minute, 0 = default

    @field_validator("ring_targets", "allowed_callers")
    @classmethod
    def _list(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x and x.strip()]

    @field_validator("dial_digit")
    @classmethod
    def _dial_digit(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) != 1 or v not in "0123456789":
            raise ValueError("one digit")
        return v

    @field_validator("menu_digit")
    @classmethod
    def _menu_digit(cls, v: str) -> str:
        v = (v or "").strip()
        if v and (len(v) != 1 or v not in DTMF_KEYS):
            raise ValueError("one key (0-9, * or #) or empty")
        return v

    @field_validator("pin")
    @classmethod
    def _pin(cls, v: str) -> str:
        v = (v or "").strip()
        if v and (not v.isdigit() or not 4 <= len(v) <= 16):
            raise ValueError("a PIN is 4 to 16 digits")
        return v

    def pin_required(self, direction: Literal["outbound", "inbound"]) -> bool:
        return bool(self.pin) and (self.pin_outbound if direction == "outbound" else self.pin_inbound)

    @field_validator("country_code", "national_prefix")
    @classmethod
    def _digits(cls, v: str) -> str:
        return digits_only(v)

    def menu_contacts(self) -> list[BridgeContact]:
        return [c for c in self.contacts if c.in_menu]

    def menu_codes(self) -> list[tuple[str, BridgeContact]]:
        """IVR keys for the menu contacts: explicit ones first, then 1-9 (or 10-99 for big menus)."""
        contacts = self.menu_contacts()
        taken = {c.digit for c in contacts if c.digit}
        if self.dial_number:
            taken.add(self.dial_digit)
        auto = [c for c in contacts if not c.digit]
        two_digits = len(contacts) + (1 if self.dial_number else 0) > 9
        pool = [str(i) for i in range(10, 100)] if two_digits else [str(i) for i in range(1, 10)]
        free = iter(p for p in pool if p not in taken and not any(t.startswith(p) or p.startswith(t) for t in taken))
        codes: dict[int, str] = {}
        for c in auto:
            codes[id(c)] = next(free, "")
        out = []
        for c in contacts:
            code = c.digit or codes.get(id(c), "")
            if code:
                out.append((code, c))
        return out


class AppSettings(BaseModel):
    admin_password_hash: str = ""
    session_secret: str = Field(default_factory=lambda: secrets.token_hex(32))
    api_token: str = Field(default_factory=lambda: secrets.token_urlsafe(24))
    default_voice: str = DEFAULT_VOICE
    default_speed: int = Field(default=150, ge=60, le=400)
    ringback_style: Literal["eu", "us", "uk"] = "eu"   # ring tone a PBX caller hears while WhatsApp rings


class Config(BaseModel):
    version: int = 1
    settings: AppSettings = Field(default_factory=AppSettings)
    pbxs: list[Pbx] = []
    extensions: list[Extension] = []
    wa_accounts: list[WaAccount] = []
    bridges: list[Bridge] = []


def redact(model: BaseModel) -> dict:
    """Dump a model for the API without secrets (adds `<field>_set` flags)."""
    data = model.model_dump()
    for f in SECRET_FIELDS:
        if f in data:
            data[f + "_set"] = bool(data[f])
            data[f] = ""
    return data
