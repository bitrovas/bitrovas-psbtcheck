"""Client für Bitrovas PSBT-Check (Protokoll psbtcheck/1 über NIP-17, oder NIP-90 kind 5823).

Ablauf: PSBT senden → Rechnung (payment.request / 7000 payment-required) → zahlen (NWC-Wallet, Callback oder extern)
→ Ergebnis-JSON (check.result / 6823). Der Dienst signiert und sendet nie Transaktionen.
"""
from __future__ import annotations
import asyncio, json, os, time, uuid
from typing import Awaitable, Callable
from nostr_sdk import Keys, PublicKey, EventBuilder, Kind, Tag, Event, UnwrappedGift, nip17_make_private_msg
from .relay import Pool
from .nwc import NwcWallet

SERVICE_NPUB = "npub1p4puhq53dz7948tujxl0sr9tk99jl5kvwwnd5y3nh3anwn6hkrdqwfff5z"
SERVICE_PUBKEY = "0d43cb829168bc5a9d7c91bef80cabb14b2fd2cc73a6da1233bc7b374f57b0da"
DEFAULT_RELAYS = ["wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net", "wss://relay.snort.social", "wss://nostr.mom"]
PROTOCOL = "psbtcheck/1"
REQUEST_KIND, RESULT_KIND, FEEDBACK_KIND = 5823, 6823, 7000


class CheckError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class PaymentRequired(Exception):
    """Keine Wallet konfiguriert: Rechnung bezahlen, dann client.wait_result(check_id) aufrufen."""
    def __init__(self, info: dict):
        super().__init__(f"payment required: {info.get('amount_sats')} sats")
        self.info = info


def _load_keys(nsec: str | None) -> Keys:
    v = nsec or os.getenv("BITROVAS_NOSTR_NSEC")
    return Keys.parse(v.strip()) if v else Keys.generate()  # sonst ephemer


class PsbtCheckClient:
    """Ein Client-Objekt = eine Nostr-Identität. Ohne nsec ephemer (pro Prozess)."""

    def __init__(self, nsec: str | None = None, relays: list[str] | None = None, service_pubkey: str = SERVICE_PUBKEY,
                 nwc_uri: str | None = None, transport: str = "nip17"):
        self.keys = _load_keys(nsec)
        self.pk = self.keys.public_key().to_hex()
        self.relays = relays or [r for r in os.getenv("BITROVAS_RELAYS", "").split(",") if r] or DEFAULT_RELAYS
        self.service = PublicKey.parse(service_pubkey).to_hex()
        uri = nwc_uri if nwc_uri is not None else (os.getenv("NWC_URI") or os.getenv("BITROVAS_NWC_URI"))
        self.wallet = NwcWallet(uri) if uri else None
        if transport not in ("nip17", "nip90"):
            raise ValueError("transport must be nip17 or nip90")
        self.transport = transport
        self.pool: Pool | None = None
        self.q: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, dict] = {}  # check_id -> {transport, request_id}

    # ---------- message building / parsing (pure, unit-testbar)
    def build_request(self, psbt: str, expected_address=None, expected_amount_sats=None, network=None, real_payment=None, stamp=None) -> tuple[str, dict]:
        rid = uuid.uuid4().hex[:16]
        m = {"protocol": PROTOCOL, "type": "check.request", "id": rid, "psbt": psbt.strip(), "idempotency_key": rid}
        for k, v in (("expected_address", expected_address), ("expected_amount_sats", expected_amount_sats), ("network", network), ("real_payment", real_payment), ("stamp", stamp)):
            if v not in (None, ""):
                m[k] = v
        return rid, m

    def build_dvm_event(self, m: dict) -> dict:
        tags = [["i", m["psbt"], "text"]] + [["param", k, str(m[k]).lower() if isinstance(m[k], bool) else str(m[k])]
                                            for k in ("expected_address", "expected_amount_sats", "network", "real_payment", "stamp") if k in m]
        content = self.keys.nip44_encrypt(PublicKey.parse(self.service), json.dumps(tags))
        ev = EventBuilder(Kind(REQUEST_KIND), content).tags([Tag.parse(["p", self.service]), Tag.parse(["encrypted"])]).finalize(self.keys)
        return json.loads(ev.as_json())

    def wrap(self, obj: dict) -> dict:
        return json.loads(nip17_make_private_msg(self.keys, PublicKey.parse(self.service), json.dumps(obj)).as_json())

    def decode(self, ev: dict) -> dict | None:
        """Gift-Wrap (NIP-17) oder DVM-Event (7000/6823) des Dienstes → normalisiertes Dict."""
        try:
            if ev.get("kind") == 1059:
                ug = UnwrappedGift.from_gift_wrap(self.keys, Event.from_json(json.dumps(ev)))
                if ug.sender().to_hex() != self.service:
                    return None
                return json.loads(ug.rumor().content())
            if ev.get("pubkey") != self.service or not Event.from_json(json.dumps(ev)).verify():
                return None
            tags = ev.get("tags", [])
            req = next((t[1] for t in tags if t[0] == "e"), None)
            if ev["kind"] == RESULT_KIND:
                c = ev["content"]
                if ["encrypted"] in tags:
                    c = self.keys.nip44_decrypt(PublicKey.parse(self.service), c)
                return {**json.loads(c), "_request_id": req}
            if ev["kind"] == FEEDBACK_KIND:
                st = next((t for t in tags if t[0] == "status"), ["status", ""])
                if st[1] == "payment-required":
                    amt = next(t for t in tags if t[0] == "amount")
                    return {"type": "payment.request", "_request_id": req, "amount_sats": int(amt[1]) // 1000, "bolt11": amt[2] if len(amt) > 2 else None}
                if st[1] == "error":
                    return {"type": "error", "_request_id": req, "code": (st[2] if len(st) > 2 else "error").split(":")[0], "message": st[2] if len(st) > 2 else ""}
                return None
        except Exception:
            return None
        return None

    # ---------- network
    async def _ensure(self):
        if self.pool:
            return
        self.pool = Pool(self.relays)
        async def on(_sid, ev):
            d = self.decode(ev)
            if d:
                await self.q.put(d)
        self.pool.handlers.append(on)
        await self.pool.start()
        now = int(time.time())
        await self.pool.subscribe("ps", [{"kinds": [1059], "#p": [self.pk], "since": now - 2 * 86400},
                                         {"kinds": [FEEDBACK_KIND, RESULT_KIND], "authors": [self.service], "#p": [self.pk], "since": now - 60}])
        await asyncio.sleep(0.5)

    async def close(self):
        if self.pool:
            await self.pool.close(); self.pool = None

    async def _wait(self, match: Callable[[dict], bool], timeout: float) -> dict:
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                raise asyncio.TimeoutError
            d = await asyncio.wait_for(self.q.get(), left)
            if match(d):
                return d

    async def check(self, psbt: str, expected_address: str | None = None, expected_amount_sats: int | None = None,
                    network: str | None = None, pay: bool = True, real_payment: bool | None = None, stamp: bool | None = None,
                    on_invoice: Callable[[dict], Awaitable[None] | None] | None = None, timeout: float = 120) -> dict:
        """Gibt das Ergebnis-JSON zurück (verdict ok|warn|danger, result, text_de, text_en).
        stamp=True: zusätzlich Bitcoin-Zeitstempel des Berichts (+21 sats); Antwort enthält "report" (kanonische Bytes, unverändert speichern)
        und "timestamp" {stamp_id, digest, ots_base64 (pending)}; der finale Beleg kommt später als DM "timestamp.confirmed".
        Ohne Wallet und ohne on_invoice → PaymentRequired mit bolt11/check_id (danach wait_result)."""
        await self._ensure()
        rid, m = self.build_request(psbt, expected_address, expected_amount_sats, network, real_payment, stamp)
        if self.transport == "nip17":
            ev = self.wrap(m)
            match = lambda d: d.get("reply_to") == rid
        else:
            ev = self.build_dvm_event(m); rid = ev["id"]
            match = lambda d: d.get("_request_id") == rid
        if not await self.pool.publish(ev):
            raise ConnectionError("Senden fehlgeschlagen")
        d = await self._wait(match, 60)
        if d.get("type") == "error":
            raise CheckError(d.get("code", "error"), d.get("message", ""))
        if d.get("type") == "check.result":
            return self._clean(d)
        info = {"check_id": d.get("check_id"), "request_id": rid, "transport": self.transport, "bolt11": d["bolt11"],
                "payment_hash": d.get("payment_hash"), "amount_sats": d.get("amount_sats"), "expires_at_iso": d.get("expires_at_iso"),
                "service_npub": SERVICE_NPUB}
        key = info["check_id"] or rid
        self.pending[key] = info
        if pay and self.wallet:
            pr = await self.wallet.pay_invoice(info["bolt11"])
            info["preimage"] = pr.get("preimage")
        elif on_invoice:
            r = on_invoice(info)
            if asyncio.iscoroutine(r):
                await r
        else:
            raise PaymentRequired(info)
        return await self.wait_result(key, timeout, info.get("preimage"))

    async def wait_result(self, key: str, timeout: float = 600, preimage: str | None = None) -> dict:
        """Wartet auf das Ergebnis nach der Zahlung. Bei NIP-17 wird periodisch der Status abgefragt (mit Preimage, falls bekannt)."""
        await self._ensure()
        info = self.pending.get(key, {"check_id": key, "transport": "nip17", "request_id": None})
        end = time.time() + timeout
        while time.time() < end:
            if info["transport"] == "nip17" and info.get("check_id"):
                st = {"protocol": PROTOCOL, "type": "check.status.request", "id": uuid.uuid4().hex[:12], "check_id": info["check_id"]}
                if preimage:
                    st["preimage"] = preimage
                await self.pool.publish(self.wrap(st))
            try:
                d = await self._wait(lambda d: d.get("type") in ("check.result", "check.status", "error") and
                                     (d.get("check_id") == info.get("check_id") or (info.get("request_id") and d.get("_request_id") == info["request_id"])),
                                     min(30, max(1, end - time.time())))
            except asyncio.TimeoutError:
                continue
            if d["type"] == "check.result":
                self.pending.pop(key, None)
                return self._clean(d)
            if d["type"] == "error":
                raise CheckError(d.get("code", "error"), d.get("message", ""))
            if d.get("state") == "EXPIRED":
                raise CheckError("expired", "Rechnung abgelaufen / invoice expired")
            await asyncio.sleep(5)
        raise asyncio.TimeoutError("kein Ergebnis / no result")

    @staticmethod
    def _clean(d: dict) -> dict:
        return {k: v for k, v in d.items() if not k.startswith("_") and k not in ("protocol", "reply_to")}
