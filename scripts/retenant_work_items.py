"""Move existing work items to a tenant before binding an org key to it (#521).

Usage: CFACTORY_DATABASE_URL=... PYTHONPATH=apps/backend \
    python scripts/retenant_work_items.py --tenant acme --keys-file keys.txt \
    [--from-tenant default] [--dry-run]

keys.txt holds one correlation key per line. Rows owned by another tenant are skipped.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from cfactory.store import get_store


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--keys-file", required=True, type=Path)
    ap.add_argument("--from-tenant", default="default")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    keys = [k.strip() for k in a.keys_file.read_text().splitlines() if k.strip()]
    n = get_store().retenant(a.tenant, keys, a.from_tenant, dry_run=a.dry_run)
    print(f"{'would move' if a.dry_run else 'moved'} {n} of {len(keys)} rows")  # noqa: T201 -- CLI output


if __name__ == "__main__":
    main()
