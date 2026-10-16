import asyncio, json, sys
import pytest
from nostr_sdk import Keys, PublicKey, nip04_encrypt
import os
_SVC = os.getenv("PSBTCHECK_SERVICE_DIR", os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [_SVC, os.path.join(_SVC, "tests")]
try:
    import psbtcheck.service  # noqa: F401  (Dienstcode, nicht Teil des Pakets)
    HAVE_SVC = True
except ImportError:
    HAVE_SVC = False
needs_svc = pytest.mark.skipif(not HAVE_SVC, reason="psbtcheck service code not available")
from bitrovas_psbtcheck import PsbtCheckClient, PaymentRequired, CheckError, NwcWallet
from bitrovas_psbtcheck.nwc import parse_uri, NwcError
from bitrovas_psbtcheck.mcp_server import Server, TOOLS

SVC = Keys.generate()


class Bridge:
    """Pool-Ersatz: leitet Client-Events offline an den echten psbtcheck-Dienst (Mock-Zahlung) und zurück."""
    def __init__(self, client, tmp, test_clients=()):
        from psbtcheck.service import Orchestrator, MessageHandler
        from psbtcheck.payments import MockPaymentAdapter
        from psbtcheck.nostr_io import unwrap, wrap
        from psbtcheck import nip90
        self.c, self.pay = client, MockPaymentAdapter()
        self.orch = Orchestrator(str(tmp / "s.db"), self.pay, test_clients=test_clients)
        self.h, self.unwrap, self.wrap, self.nip90 = MessageHandler(self.orch), unwrap, wrap, nip90
        from nostr_sdk import EventBuilder, Kind, Tag
        self.EB, self.K, self.T = EventBuilder, Kind, Tag
        async def done(c):
            from psbtcheck.service import result_message
            await self._deliver(self.wrap(SVC, client.pk, result_message(c)))
        self.orch.listeners.append(done)
        self.sent = []

    async def _deliver(self, ev):
        d = self.c.decode(ev)
        if d: await self.c.q.put(d)

    async def publish(self, ev):
        self.sent.append(ev)
        if ev["kind"] == 1059:
            sender, text = self.unwrap(SVC, ev)
            for r in await self.h.handle(sender, text):
                await self._deliver(self.wrap(SVC, sender, r))
        elif ev["kind"] == 5823:
            inner = json.loads(SVC.nip44_decrypt(PublicKey.parse(ev["pubkey"]), ev["content"]))
            for spec in await self.nip90.handle_request(self.orch, ev["pubkey"], ev["id"], inner):
                content = SVC.nip44_encrypt(PublicKey.parse(ev["pubkey"]), spec["content"]) if spec.get("encrypt") else spec["content"]
                e = json.loads(self.EB(self.K(spec["kind"]), content).tags([self.T.parse(t) for t in spec["tags"]]).finalize(SVC).as_json())
                await self._deliver(e)
        return 1

    async def subscribe(self, *a): pass
    async def close(self): pass


def mk(tmp, transport="nip17", tests=False):
    c = PsbtCheckClient(service_pubkey=SVC.public_key().to_hex(), nwc_uri="", transport=transport)
    b = Bridge(c, tmp, {c.pk} if tests else ())
    c.pool = b
    return c, b


def psbt():
    from psbtgen import sample
    return sample()


async def test_ephemeral_key_and_no_secret_in_package():
    import pathlib, re
    a, b = PsbtCheckClient(nwc_uri=""), PsbtCheckClient(nwc_uri="")
    assert a.pk != b.pk
    src = "".join(p.read_text() for p in pathlib.Path(__file__).parents[1].joinpath("bitrovas_psbtcheck").glob("*.py"))
    assert not re.search(r"nsec1[0-9a-z]{20,}", src) and "secret=" not in src.replace('"NWC-URI braucht relay= und secret="', "")
    assert not re.search(r"[\w.+-]+@[\w-]+\.(com|ch|de|net|org)", src)  # keine E-Mail-Adressen


@needs_svc
async def test_nip17_payment_required_then_result(tmp_path):
    c, b = mk(tmp_path)
    with pytest.raises(PaymentRequired) as e:
        await c.check(psbt(), expected_amount_sats=60000)
    info = e.value.info
    assert info["amount_sats"] == 21 and info["bolt11"].startswith("lnbcrt") and info["check_id"]
    b.pay.simulate_payment(info["payment_hash"]); await b.orch.poll_once()
    r = await c.wait_result(info["check_id"], timeout=5)
    assert r["verdict"] == "ok" and r["type"] == "check.result" and "text_de" in r


@needs_svc
async def test_on_invoice_callback(tmp_path):
    c, b = mk(tmp_path)
    async def payer(info):
        b.pay.simulate_payment(info["payment_hash"]); await b.orch.poll_once()
    r = await c.check(psbt(), on_invoice=payer, timeout=5)
    assert r["verdict"] == "ok"


@needs_svc
async def test_nip90_flow(tmp_path):
    c, b = mk(tmp_path, "nip90")
    with pytest.raises(PaymentRequired) as e:
        await c.check(psbt(), network="mainnet")
    assert e.value.info["amount_sats"] == 21 and e.value.info["check_id"] is None


@needs_svc
async def test_nwc_wallet_pays(tmp_path):
    c, b = mk(tmp_path)
    class W:
        async def pay_invoice(self, inv):
            ph = [x for x in b.orch.db.execute("SELECT payment_hash FROM checks WHERE bolt11=?", (inv,))][0][0]
            pre = b.pay.simulate_payment(ph); return {"preimage": pre}
    c.wallet = W()
    r = await c.check(psbt(), stamp=None, timeout=5)
    assert r["verdict"] == "ok"


@needs_svc
async def test_testmode_direct_result(tmp_path):
    c, b = mk(tmp_path, tests=True)
    assert (await c.check(psbt()))["verdict"] == "ok"


@needs_svc
async def test_error_invalid(tmp_path):
    c, b = mk(tmp_path)
    with pytest.raises(CheckError) as e:
        await c.check("cHNidP8BAAAA")
    assert e.value.code == "invalid_psbt"


def test_request_params():
    c = PsbtCheckClient(nwc_uri="")
    _, m = c.build_request(" abc ", "bc1qx", 5, "testnet", True, True)
    assert m["psbt"] == "abc" and m["stamp"] is True and m["real_payment"] is True and m["protocol"] == "psbtcheck/1"
    ev = c.build_dvm_event(m)
    assert ev["kind"] == 5823 and ["encrypted"] in ev["tags"] and "abc" not in ev["content"]


def test_nwc_uri_and_roundtrip():
    w_keys, app = Keys.generate(), Keys.generate()
    uri = f"nostr+walletconnect://{w_keys.public_key().to_hex()}?relay=wss%3A%2F%2Frelay.example&secret={app.secret_key().to_hex()}"
    w = NwcWallet(uri)
    assert w.relays == ["wss://relay.example"] and app.secret_key().to_hex() not in repr(w)
    req = w.build_request("lnbc1")
    assert req["kind"] == 23194 and ["p", w_keys.public_key().to_hex()] in req["tags"]
    from nostr_sdk import nip04_decrypt
    body = json.loads(nip04_decrypt(w_keys.secret_key(), app.public_key(), req["content"]))
    assert body == {"method": "pay_invoice", "params": {"invoice": "lnbc1"}}
    resp = {"content": nip04_encrypt(w_keys.secret_key(), app.public_key(), json.dumps({"result_type": "pay_invoice", "result": {"preimage": "00"}}))}
    assert w.parse_response(resp)["result"]["preimage"] == "00"
    with pytest.raises(NwcError): parse_uri("https://x")


async def test_mcp_protocol(monkeypatch):
    s = Server()
    r = await s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert r["result"]["serverInfo"]["name"] == "bitrovas-psbtcheck" and r["result"]["protocolVersion"] == "2025-06-18"
    assert await s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [t["name"] for t in (await s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))["result"]["tools"]]
    assert names == ["psbt_check", "psbt_check_result"]
    async def fake(self, *a, **k): raise PaymentRequired({"check_id": "c1", "request_id": "r", "bolt11": "lnbc", "amount_sats": 21})
    monkeypatch.setattr(PsbtCheckClient, "check", fake)
    monkeypatch.delenv("NWC_URI", raising=False); monkeypatch.delenv("BITROVAS_NWC_URI", raising=False)
    r = await s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "psbt_check", "arguments": {"psbt": "x"}}})
    out = json.loads(r["result"]["content"][0]["text"])
    assert out["status"] == "payment_required" and out["bolt11"] == "lnbc" and "c1" in s.by_check
    r = await s.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert r["error"]["code"] == -32601


def test_mcp_stdio_subprocess():
    import subprocess
    p = subprocess.run([sys.executable, "-m", "bitrovas_psbtcheck.mcp_server"], input=
                       '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
                       capture_output=True, text=True, timeout=30)
    lines = [json.loads(l) for l in p.stdout.splitlines()]
    assert {l["id"] for l in lines} == {1, 2}
