"""NIP-47 Nostr Wallet Connect: pay_invoice über die Wallet des Nutzers (URI aus NWC_URI / BITROVAS_NWC_URI)."""
from __future__ import annotations
import asyncio, json, time
from urllib.parse import urlparse, parse_qs
from nostr_sdk import Keys, PublicKey, SecretKey, EventBuilder, Kind, Tag, nip04_encrypt, nip04_decrypt
from .relay import Pool


class NwcError(RuntimeError):
    pass


def parse_uri(uri: str) -> tuple[str, list[str], str]:
    u = urlparse(uri.strip())
    if u.scheme not in ("nostr+walletconnect", "nostrwalletconnect"):
        raise NwcError("keine NWC-URI (nostr+walletconnect://...)")
    q = parse_qs(u.query)
    wallet = u.netloc or u.path.lstrip("/")
    if not q.get("relay") or not q.get("secret"):
        raise NwcError("NWC-URI braucht relay= und secret=")
    return PublicKey.parse(wallet).to_hex(), q["relay"], q["secret"][0]


class NwcWallet:
    def __init__(self, uri: str):
        self.wallet, self.relays, secret = parse_uri(uri)
        self.keys = Keys(SecretKey.parse(secret))

    def __repr__(self):  # nie das Secret ausgeben
        return f"NwcWallet(wallet={self.wallet[:8]}…, relays={self.relays})"

    def build_request(self, invoice: str) -> dict:
        content = nip04_encrypt(self.keys.secret_key(), PublicKey.parse(self.wallet),
                                json.dumps({"method": "pay_invoice", "params": {"invoice": invoice}}))
        ev = EventBuilder(Kind(23194), content).tags([Tag.parse(["p", self.wallet])]).finalize(self.keys)
        return json.loads(ev.as_json())

    def parse_response(self, ev: dict) -> dict:
        return json.loads(nip04_decrypt(self.keys.secret_key(), PublicKey.parse(self.wallet), ev["content"]))

    async def pay_invoice(self, invoice: str, timeout: float = 60) -> dict:
        req = self.build_request(invoice)
        pool = Pool(self.relays); q = asyncio.Queue()
        async def on(_sid, ev):
            if ev.get("kind") == 23195 and ev.get("pubkey") == self.wallet and ["e", req["id"]] in [t[:2] for t in ev.get("tags", [])]:
                await q.put(ev)
        pool.handlers.append(on)
        try:
            await pool.start()
            await pool.subscribe("nwc", [{"kinds": [23195], "authors": [self.wallet], "#e": [req["id"]], "since": int(time.time()) - 10}])
            await pool.publish(req)
            res = self.parse_response(await asyncio.wait_for(q.get(), timeout))
        except asyncio.TimeoutError:
            raise NwcError("Wallet antwortet nicht (NWC timeout)")
        finally:
            await pool.close()
        if res.get("error"):
            raise NwcError(f"Wallet-Fehler: {res['error'].get('code')} {res['error'].get('message', '')}")
        return res.get("result") or {}
