"""Generate the VAPID key pair ESRMS uses to sign web push messages.

    .venv\\Scripts\\python scripts\\generate_vapid_keys.py --subject mailto:you@your-domain.example --write

Without --write the three lines are only printed, for you to paste into .env.
With --write they are appended to .env, unless VAPID keys are already there.

Run this once. The private key must stay on the server; if it changes, every device has to
switch push on again. The subject is a contact address the push services (Google, Mozilla,
Apple, Microsoft) can use if your server misbehaves.
"""
import argparse
import base64
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate():
    key = ec.generate_private_key(ec.SECP256R1())
    private = b64url(key.private_numbers().private_value.to_bytes(32, "big"))
    public = b64url(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    return public, private


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--subject", required=True, help="mailto:you@your-domain.example or https://your-site")
    parser.add_argument("--write", action="store_true", help="append the keys to .env")
    args = parser.parse_args()
    if not args.subject.startswith(("mailto:", "https://")):
        sys.exit("--subject must start with mailto: or https://")

    public, private = generate()
    lines = f"VAPID_PUBLIC_KEY={public}\nVAPID_PRIVATE_KEY={private}\nVAPID_SUBJECT={args.subject}\n"
    if not args.write:
        print(lines, end="")
        return
    existing = open(ENV, encoding="utf-8").read() if os.path.exists(ENV) else ""
    if any(line.startswith("VAPID_PRIVATE_KEY=") and line.strip() != "VAPID_PRIVATE_KEY=" for line in existing.splitlines()):
        sys.exit(".env already has VAPID keys. Remove them first if you really want new ones "
                 "(every device will have to switch push on again).")
    kept = [line for line in existing.splitlines() if not line.startswith(("VAPID_PUBLIC_KEY=", "VAPID_PRIVATE_KEY=", "VAPID_SUBJECT="))]
    with open(ENV, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept).rstrip("\n") + "\n\n# Web push (VAPID)\n" + lines)
    print("VAPID keys written to .env. Restart ESRMS to use them.")


if __name__ == "__main__":
    main()
