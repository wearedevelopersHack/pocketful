"""Run the Pocketful API: ``python3 -m api --db <path> [--host H] [--port P]``.

Plan §1.3 applies here: the database path is a real path on the durable volume
(never ``/tmp`` on prod), and nothing on this path shells out to a ``sqlite3``
CLI, because neither host has one.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .app import LOG, create_server

DEFAULT_DB = os.environ.get("POCKETFUL_DB", "pocketful.db")
DEFAULT_HOST = os.environ.get("POCKETFUL_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("POCKETFUL_PORT", "8000"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api", description="Pocketful HTTP API")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="path to the ledger database file (default: POCKETFUL_DB)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    server = create_server(args.db, host=args.host, port=args.port)
    LOG.info("listening on http://%s:%s (db=%s)",
             server.server_address[0], server.server_address[1], args.db)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
