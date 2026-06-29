#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFIS -- Generate authentication config for streamlit-authenticator.

Usage
-----
# Interactive: creates auth_config.yaml in the repo root
python scripts/make_auth_config.py

# Print the base64-encoded value for `fly secrets set`
python scripts/make_auth_config.py --b64

# One-liner to push directly to Fly.io (bash/mac/linux only)
fly secrets set FFIS_AUTH_CONFIG="$(python scripts/make_auth_config.py --b64)"

# Windows PowerShell two-step:
python scripts/make_auth_config.py --b64 | Out-File -NoNewline -Encoding utf8 ffis_auth_b64.txt
fly secrets set FFIS_AUTH_CONFIG=(Get-Content ffis_auth_b64.txt) --app ffis-scrubber
Remove-Item ffis_auth_b64.txt

Credentials live ONLY in Fly secrets (or auth_config.yaml which is gitignored).
They are never committed to the repository.
"""

import argparse
import base64
import getpass
import os
import secrets
import sys
from pathlib import Path

# Force UTF-8 output on Windows so Unicode characters don't crash cp1252
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import bcrypt
    import yaml
except ImportError:
    sys.exit("Missing deps -- run:  pip install bcrypt PyYAML")


REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "auth_config.yaml"


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash of the password (streamlit-authenticator format)."""
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def prompt_users() -> dict:
    """Interactively collect username / display-name / password triples."""
    users = {}
    print("\n--- Add users ---------------------------------------------------")
    print("Press Enter with a blank username when done.\n")
    while True:
        username = input("  Username (no spaces, e.g. 'alice'): ").strip()
        if not username:
            break
        if " " in username:
            print("  X  Username cannot contain spaces -- try again.")
            continue
        display_name = input(f"  Display name for '{username}': ").strip() or username
        password = getpass.getpass(f"  Password for '{username}': ")
        if not password:
            print("  X  Password cannot be blank -- skipping this user.")
            continue
        users[username] = {
            "name": display_name,
            "password": hash_password(password),
        }
        print(f"  OK  Added '{username}'\n")
    return users


def build_config(users: dict) -> dict:
    """Assemble the full YAML structure."""
    cookie_key = secrets.token_hex(24)   # 48 random hex chars
    return {
        "credentials": {
            "usernames": users,
        },
        "cookie": {
            "name": "ffis_auth",
            "key": cookie_key,
            "expiry_days": 7,
        },
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--b64",
        action="store_true",
        help="Print base64-encoded config to stdout (for fly secrets set)",
    )
    args = parser.parse_args()

    # Check for existing config
    if OUTPUT_PATH.exists() and not args.b64:
        overwrite = input("\nauth_config.yaml already exists. Overwrite? [y/N] ").strip().lower()
        if overwrite != "y":
            sys.exit("Aborted.")

    users = prompt_users()
    if not users:
        sys.exit("No users added -- aborted.")

    cfg = build_config(users)
    yaml_str = yaml.dump(cfg, default_flow_style=False, allow_unicode=True)

    if args.b64:
        # Write raw bytes to stdout buffer to avoid any encoding translation
        sys.stdout.buffer.write(base64.b64encode(yaml_str.encode()))
    else:
        with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
            fh.write(yaml_str)
        print(f"\nOK  Written to {OUTPUT_PATH}")
        print("\nNext steps:")
        print("  Local dev:  auth_config.yaml is gitignored -- use it as-is.")
        print("  Fly.io deploy (PowerShell):")
        print("    python scripts/make_auth_config.py --b64 | Out-File -NoNewline -Encoding utf8 ffis_auth_b64.txt")
        print("    fly secrets set FFIS_AUTH_CONFIG=(Get-Content ffis_auth_b64.txt) --app ffis-scrubber")
        print("    Remove-Item ffis_auth_b64.txt")
        print("\nTo add more users later, re-run this script.")


if __name__ == "__main__":
    main()