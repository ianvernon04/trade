"""`python3 -m onlymeyou` — start the app and print phone-friendly URLs.

Binds 0.0.0.0 by default on purpose: the whole point is opening it on two
phones. The banner nudges toward setting a PIN for that reason, and the
README covers getting an HTTPS URL (needed for in-app mic recording and
full home-screen install).
"""

from __future__ import annotations

import argparse
import socket


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet leaves; picks a route
            return s.getsockname()[0]
    except OSError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="python3 -m onlymeyou",
                                     description="Run Only Me & You 💞")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - depends on the machine
        raise SystemExit("uvicorn is missing — run: pip install -r requirements.txt")

    lan = _lan_ip()
    print()
    print("  💞  Only Me & You")
    print(f"      on this computer:  http://127.0.0.1:{args.port}")
    if lan and args.host == "0.0.0.0":
        print(f"      on your phones:    http://{lan}:{args.port}   (same Wi-Fi)")
    print("      open it on both phones → Share → Add to Home Screen")
    print("      tip: set a PIN in setup — anyone on your network can reach this")
    print()
    uvicorn.run("onlymeyou.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
