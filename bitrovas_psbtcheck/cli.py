"""CLI: bitrovas-psbtcheck <psbt|@datei> [--expected ADR] [--amount SATS] [--network N] [--nip90] [--no-pay]"""
import argparse, asyncio, json, sys
from .client import PsbtCheckClient, PaymentRequired


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bitrovas-psbtcheck")
    ap.add_argument("psbt"); ap.add_argument("--expected"); ap.add_argument("--amount", type=int); ap.add_argument("--network")
    ap.add_argument("--nip90", action="store_true"); ap.add_argument("--no-pay", action="store_true"); ap.add_argument("--stamp", action="store_true"); ap.add_argument("--real-payment", action="store_true"); ap.add_argument("--timeout", type=float, default=600)
    a = ap.parse_args(argv)
    psbt = open(a.psbt[1:]).read() if a.psbt.startswith("@") else a.psbt
    async def run():
        c = PsbtCheckClient(transport="nip90" if a.nip90 else "nip17")
        if a.no_pay: c.wallet = None
        try:
            async def show(info):
                print(json.dumps({"status": "payment_required", **info}), file=sys.stderr)
                print(f"Bitte bezahlen / please pay: {info['bolt11']}", file=sys.stderr)
            r = await c.check(psbt, a.expected, a.amount, a.network, stamp=a.stamp or None, real_payment=a.real_payment or None, on_invoice=None if a.no_pay else show, timeout=a.timeout)
            print(json.dumps(r, ensure_ascii=False, indent=1))
        except PaymentRequired as e:
            print(json.dumps({"status": "payment_required", **e.info})); return 2
        finally:
            await c.close()
        return 0
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
