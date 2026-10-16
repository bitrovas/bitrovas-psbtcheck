"""MCP-Server (stdio, JSON-RPC 2.0, ohne Zusatzpakete) mit den Tools psbt_check und psbt_check_result.

Konfiguration nur über Umgebungsvariablen des Nutzers: NWC_URI (optional, eigene Wallet), BITROVAS_NOSTR_NSEC (optional,
sonst ephemerer Schlüssel pro Serverprozess), BITROVAS_RELAYS (optional). Ohne Wallet liefert psbt_check die Rechnung zurück;
nach der Zahlung psbt_check_result(check_id) aufrufen (gleicher Serverprozess).
"""
from __future__ import annotations
import asyncio, json, sys
from . import __version__
from .client import PsbtCheckClient, PaymentRequired, CheckError

TOOLS = [
    {"name": "psbt_check",
     "description": ("Bitrovas PSBT-Check: prüft eine UNSIGNIERTE Bitcoin-PSBT vor dem Signieren (Empfänger, Beträge, Change, Gebühr/sat/vB, "
                     "RBF, Sighash, UTXO-Infos, Dust, Address-Poisoning-Lookalikes). Kostet 21 sats (Lightning). Mit NWC-Wallet wird automatisch "
                     "bezahlt und das Ergebnis (verdict ok|warn|danger + JSON + Text DE/EN) zurückgegeben; ohne Wallet kommt status=payment_required "
                     "mit bolt11 – nach Zahlung psbt_check_result aufrufen. Signiert und sendet nie etwas."),
     "inputSchema": {"type": "object", "required": ["psbt"], "properties": {
         "psbt": {"type": "string", "description": "unsignierte PSBT v0, base64 oder hex"},
         "expected_address": {"type": "string", "description": "erwartete Empfängeradresse (optional)"},
         "expected_amount_sats": {"type": "integer", "description": "erwarteter Betrag an diese Adresse in sats (optional)"},
         "network": {"type": "string", "enum": ["mainnet", "testnet", "signet", "regtest"]},
         "transport": {"type": "string", "enum": ["nip17", "nip90"], "default": "nip17"},
         "stamp": {"type": "boolean", "default": False, "description": "Bericht zusätzlich mit Bitcoin-Zeitstempel belegen (+21 sats, total 42)"},
         "pay": {"type": "boolean", "default": True, "description": "false = nie automatisch zahlen, nur Rechnung zurückgeben"}}}},
    {"name": "psbt_check_result",
     "description": "Holt das Ergebnis eines PSBT-Checks nach Zahlung der Rechnung (check_id aus psbt_check).",
     "inputSchema": {"type": "object", "required": ["check_id"], "properties": {
         "check_id": {"type": "string"}, "preimage": {"type": "string", "description": "optionaler Zahlungsnachweis"},
         "wait_seconds": {"type": "integer", "default": 60}}}},
]


class Server:
    def __init__(self):
        self.clients: dict[str, PsbtCheckClient] = {}
        self.by_check: dict[str, PsbtCheckClient] = {}

    def client(self, transport: str) -> PsbtCheckClient:
        if transport not in self.clients:
            base = next(iter(self.clients.values()), None)
            c = PsbtCheckClient(transport=transport)
            if base:  # gleiche Identität für beide Transporte
                c.keys, c.pk = base.keys, base.pk
            self.clients[transport] = c
        return self.clients[transport]

    async def call(self, name: str, a: dict) -> dict:
        if name == "psbt_check":
            c = self.client(a.get("transport", "nip17"))
            try:
                r = await c.check(a["psbt"], a.get("expected_address"), a.get("expected_amount_sats"), a.get("network"), stamp=a.get("stamp"),
                                  pay=a.get("pay", True) and c.wallet is not None)
                return {"status": "ok", **r}
            except PaymentRequired as e:
                self.by_check[e.info.get("check_id") or e.info["request_id"]] = c
                return {"status": "payment_required", **e.info,
                        "next": "Rechnung bezahlen, dann psbt_check_result mit check_id aufrufen / pay the invoice, then call psbt_check_result"}
        if name == "psbt_check_result":
            c = self.by_check.get(a["check_id"]) or self.client("nip17")
            r = await c.wait_result(a["check_id"], float(a.get("wait_seconds", 60)), a.get("preimage"))
            return {"status": "ok", **r}
        raise KeyError(name)

    async def handle(self, req: dict) -> dict | None:
        mid, method = req.get("id"), req.get("method")
        if mid is None:
            return None  # Notification
        try:
            if method == "initialize":
                res = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "bitrovas-psbtcheck", "version": __version__}}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                p = req.get("params", {})
                try:
                    out = await self.call(p.get("name"), p.get("arguments") or {})
                    res = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}], "isError": False}
                except KeyError as e:
                    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool or missing argument {e}"}}
                except (CheckError, asyncio.TimeoutError, ConnectionError, Exception) as e:  # noqa: BLE001
                    res = {"content": [{"type": "text", "text": json.dumps({"status": "error", "error": str(e) or type(e).__name__})}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
            return {"jsonrpc": "2.0", "id": mid, "result": res}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


async def serve(stdin=None, stdout=None):
    loop = asyncio.get_running_loop()
    rd = asyncio.StreamReader(limit=2**22)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(rd), stdin or sys.stdin)
    srv = Server()
    out = stdout or sys.stdout
    async def one(line):
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = await srv.handle(req)
        if resp:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n"); out.flush()
    tasks = set()
    while line := await rd.readline():
        if line.strip():
            t = asyncio.create_task(one(line)); tasks.add(t); t.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.gather(*tasks)
    for c in srv.clients.values():
        await c.close()


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
