#!/usr/bin/env python3
"""Backup and verify a Pocketful SQLite database. Runs ON the prod host.

Why this exists rather than a `cp` or a shell one-liner:

  * There is no `sqlite3` CLI on either host (plan §1.3.2), so the backup is
    driven from the stdlib `sqlite3` module.
  * With WAL enabled, copying the `.db` file alone is NOT a backup. Measured on
    this host: a plain copy, taken while WAL was live, restored to
    ``OperationalError: no such table: ledger_entries`` - no schema at all,
    because the CREATE TABLE statements were still in the `-wal` file. It looks
    like a successful copy and restores as an empty database.

Usage:
    python3 ops/backup_db.py backup  --db /var/lib/pocketful/pocketful.db \\
                                     --out /var/backups/pocketful/pocketful-<ts>.db
    python3 ops/backup_db.py verify  --backup /var/backups/pocketful/pocketful-<ts>.db
    python3 ops/backup_db.py restore --backup <file> --db <target>   # target must not exist

Every mode prints what it did. `verify` opens the backup and runs
`PRAGMA integrity_check` plus row counts; it does not assume the copy worked.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

MONEY_TABLES = ("accounts", "transfers", "ledger_entries", "idempotency_keys")


def cmd_backup(args: argparse.Namespace) -> int:
    if not os.path.exists(args.db):
        print(f"backup: source does not exist: {args.db}", file=sys.stderr)
        return 1
    if os.path.exists(args.out):
        print(f"backup: refusing to overwrite existing {args.out}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    src = sqlite3.connect(args.db)
    try:
        # VACUUM INTO emits a single consistent file; it needs no -wal/-shm
        # companion and no quiesced writer.
        src.execute("VACUUM INTO ?", (args.out,))
    finally:
        src.close()

    size = os.path.getsize(args.out)
    print(f"backup ok: {args.out} ({size} bytes)")
    return cmd_verify(args.out)


def cmd_verify(backup_path: str) -> int:
    conn = sqlite3.connect(backup_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"integrity_check: {integrity}")
        if integrity != "ok":
            return 1
        total = 0
        for table in MONEY_TABLES:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError as exc:
                print(f"  {table}: MISSING ({exc})")
                return 1
            total += n
            print(f"  {table}: {n} rows")
        money = conn.execute(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries").fetchone()[0]
        print(f"ledger_entries sum: {money} (must be 0 - double entry)")
        if money != 0:
            print("verify: ledger does not sum to zero", file=sys.stderr)
            return 1
        print(f"verify ok: {total} rows across {len(MONEY_TABLES)} tables")
        return 0
    finally:
        conn.close()


def cmd_restore(args: argparse.Namespace) -> int:
    if os.path.exists(args.db):
        print(f"restore: refusing to overwrite live database {args.db}", file=sys.stderr)
        return 1
    if not os.path.exists(args.backup):
        print(f"restore: no such backup {args.backup}", file=sys.stderr)
        return 1
    # Copy through Python, not `cp`: the backup is a single consistent file, so
    # a byte copy of it is safe in a way a copy of a live WAL database is not.
    with open(args.backup, "rb") as src, open(args.db, "wb") as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    print(f"restore ok: {args.backup} -> {args.db}")
    return cmd_verify(args.db)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    b = sub.add_parser("backup", help="VACUUM INTO a new backup file, then verify it")
    b.add_argument("--db", required=True)
    b.add_argument("--out", required=True)
    b.set_defaults(func=cmd_backup)

    v = sub.add_parser("verify", help="integrity_check + row counts on a backup")
    v.add_argument("--backup", required=True)
    v.set_defaults(func=lambda a: cmd_verify(a.backup))

    r = sub.add_parser("restore", help="restore a backup to a non-existent path")
    r.add_argument("--backup", required=True)
    r.add_argument("--db", required=True)
    r.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
