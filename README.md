# bitrovas-psbtcheck – Agent-SDK + MCP-Tool `psbt_check`

Prüft eine **unsignierte** Bitcoin-PSBT über den Nostr-Dienst **Bitrovas PSBT-Check**
([npub1p4puhq53…](https://primal.net/p/npub1p4puhq53dz7948tujxl0sr9tk99jl5kvwwnd5y3nh3anwn6hkrdqwfff5z)) – NIP-17-DM (`psbtcheck/1`) oder NIP-90 kind 5823.
21 sats pro Check (+21 mit `stamp`), Ergebnis: `verdict` ok|warn|danger, volles JSON, Text DE/EN. Der Dienst signiert und sendet nie.

**Keine Geheimnisse im Paket.** Nostr-Schlüssel: ephemer pro Prozess oder `BITROVAS_NOSTR_NSEC` des Nutzers.
Wallet: `NWC_URI` (oder `BITROVAS_NWC_URI`) des Nutzers – NIP-47 `pay_invoice`. Ohne Wallet: Rechnung zurück (Callback / `payment_required`).

## Grenzen / Limitations (Stand 0.1.0)
- **NWC**: nur NIP-04-Verschlüsselung (Wallets, die ausschliesslich NIP-44 können, werden noch nicht unterstützt) und **nur offline getestet** (kein Live-Test mit echter Wallet).
- **Keine JS-Variante** – nur Python.
- **Live getestet**: NIP-17-Pfad mit manueller Zahlung (`on_invoice` → Rechnung extern bezahlt → `wait_result` → Ergebnis), 16.10.2026; NIP-90 und MCP live bis Ergebnis/Rechnung.
- Ergebnis ist eine automatische Prüfung ohne Gewähr; der Dienst signiert und sendet nie.
- Mit `stamp` kommt der finale Zeitstempel-Beleg später als DM an die Client-Identität – dafür einen eigenen Schlüssel (`BITROVAS_NOSTR_NSEC`) nutzen, nicht den ephemeren.

*EN:* NWC is NIP-04 only and tested offline only; no JS version; the live path with manual payment (`on_invoice`) is live-tested.

## Install (lokal)
```bash
pip install git+https://github.com/bitrovas/bitrovas-psbtcheck.git
```

## Python
```python
import asyncio
from bitrovas_psbtcheck import PsbtCheckClient, PaymentRequired
async def main():
    c = PsbtCheckClient()                       # NWC_URI aus der Umgebung → zahlt automatisch
    try:
        r = await c.check(psbt_b64, expected_address="bc1q…", expected_amount_sats=60000, stamp=False)
        print(r["verdict"], r["text_de"])
    except PaymentRequired as e:                # keine Wallet: Rechnung selbst zahlen
        print(e.info["bolt11"]); r = await c.wait_result(e.info["check_id"], timeout=600)
    finally:
        await c.close()
asyncio.run(main())
```
`on_invoice=callback` (sync/async) statt Exception: Rechnung an eigene Logik geben, danach wird auf das Ergebnis gewartet.
`transport="nip90"` für DVM kind 5823 (Ergebnis über 6823; Polling-Status nur bei NIP-17).

## CLI
`bitrovas-psbtcheck <psbt|@datei> [--expected ADR] [--amount SATS] [--network N] [--stamp] [--nip90] [--no-pay]`
(Exit 2 + JSON `payment_required`, wenn nicht bezahlt wird.)

## MCP (stdio)
```json
{"mcpServers": {"bitrovas-psbtcheck": {"command": "bitrovas-psbtcheck-mcp", "env": {"NWC_URI": "nostr+walletconnect://…"}}}}
```
Tools: `psbt_check` {psbt, expected_address?, expected_amount_sats?, network?, transport?, stamp?, pay?} und
`psbt_check_result` {check_id, preimage?, wait_seconds?}. Ohne Wallet liefert `psbt_check` `status=payment_required` + bolt11;
nach Zahlung `psbt_check_result` im selben Serverprozess (ephemerer Schlüssel bleibt im Prozess).

## Tests
`python -m pytest -q` – offline: NWC-Verschlüsselung, Request-Bau, MCP-Protokoll + stdio. Integrations-Tests gegen den Dienstcode laufen nur, wenn dieser lokal vorhanden ist (sonst übersprungen).
