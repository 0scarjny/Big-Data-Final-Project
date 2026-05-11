"""Pre-hash a password for streamlit-authenticator.

Usage:
    python hash_password.py 'your-plain-password'
    # or interactive (password is read without echoing):
    python hash_password.py

Paste the printed bcrypt hash into `.streamlit/secrets.toml` under
`[auth.credentials.usernames.<user>].password`, then set
`auth.auto_hash = false` in the same file. This avoids storing plaintext
passwords on disk.

Implementation note: we call `bcrypt` directly rather than going through
`streamlit_authenticator.Hasher`, because in v0.4+ the `Hasher` API operates
on a credentials dict in place rather than hashing a single string. bcrypt
is the same algorithm the library uses internally and is already installed
as a transitive dependency of streamlit-authenticator.
"""
from __future__ import annotations

import getpass
import sys

import bcrypt


def main() -> None:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = getpass.getpass("Password to hash: ")
    if not password:
        print("error: empty password", file=sys.stderr)
        sys.exit(1)
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    print(hashed)


if __name__ == "__main__":
    main()

# Run with: python -m weather-monitor.web-dashboard.hash_password
