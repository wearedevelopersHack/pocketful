# syntax=docker/dockerfile:1
# Pocketful — verification container.
#
# The application is Python-3 standard library only. There is no requirements.txt,
# no lockfile, and deliberately no install step below: an install step would be a
# claim that something outside the stdlib is required, which is precisely what
# this image exists to disprove. The interpreter is the whole dependency set.
#
#   Build:  docker build -t pocketful .
#   Run:    docker run --rm --network none -p 8080:8080 pocketful
#
# --network none is the point of the run, not an optimisation: it is what makes
# "no dependency reaches out" a measured fact rather than a reading of the
# imports.

FROM python:3.14-slim

# Only the shipped trees. tests/ is deliberately absent — the release tree is
# ledger/ api/ app/ (ops/README.md, "Layout"), and copying the harness into the
# image would let a container pass on files that never ship to the host.
WORKDIR /srv/pocketful
COPY ledger/ ./ledger/
COPY api/    ./api/
COPY app/    ./app/

# State lives on a volume, never in the image. The pending-key store is state
# and not litter: if it is lost, the next retry mints a fresh idempotency key,
# and a fresh key is a brand-new transfer — a double-spend that arrives past
# every ledger invariant because it is a filesystem failure, not a ledger one.
RUN mkdir -p /var/lib/pocketful
VOLUME ["/var/lib/pocketful"]

ENV POCKETFUL_DB=/var/lib/pocketful/pocketful.db \
    POCKETFUL_UI_STORE=/var/lib/pocketful/ui-pending.json \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The two-process shape the runbook specifies (ops/README.md:72-73): the API on
# the loopback, the wallet UI in front of it. The UI is the foreground process,
# so the container's lifetime is the UI's; the API is backgrounded behind it.
# This is a verification container, not a production supervisor — systemd owns
# that job on the host.
#
# The API binds the loopback only. The UI binds 0.0.0.0 so the port mapping can
# reach it; nothing else in the container listens on a routable address.
EXPOSE 8080
CMD ["sh", "-c", "python3 -m api --db \"$POCKETFUL_DB\" --host 127.0.0.1 --port 8001 & sleep 1; exec python3 -m app.web --api http://127.0.0.1:8001 --store \"$POCKETFUL_UI_STORE\" --host 0.0.0.0 --port 8080"]
