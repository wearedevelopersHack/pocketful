# Pocketful

Pocketful is our Band Desktop factory submission for the Pocketful track of the
Dark Factory hackathon. Felix Nguyen, Priyanshu Ojha, and Ghost collaborated on
the repository. The factory's active submission room has three Codex ACP seats:
API Engineer, Reviewer, and UI Designer. See [FACTORY.md](FACTORY.md) for their
roles, run procedure, handoffs, and costs, [mandates/](mandates/) for generic
seat instructions, and `room.json` for the full Band session export.

Completed stage folders are self-contained submissions. Build and start one
using its `Dockerfile` and `RUN.md`. The original Python demo in the repository
root is separate background work; its API contract predates the official stage
specification.

## Run the submitted app

From the repository root, use the final stage:

```powershell
cd .\stage-4
docker build -t pocketful-stage-4 .
docker run --rm -p 8080:8080 pocketful-stage-4
```

Open <http://localhost:8080/>. The service keeps its state in memory, so a new
container starts empty. For a repeatable browser walkthrough, seed two fictional
users in another PowerShell terminal:

```powershell
$fixture = @{
  currency = 'EUR'
  minor_units = 2
  users = @(
    @{ id = 'u_ada'; email = 'ada@example.com'; display_name = 'Ada'; handle = 'ada'; password = 'password123'; balance = 100000 },
    @{ id = 'u_bob'; email = 'bob@example.com'; display_name = 'Bob'; handle = 'bob'; password = 'password123'; balance = 5000 }
  )
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri 'http://localhost:8080/_test/reset' -ContentType 'application/json' -Body $fixture
```

Sign in as Ada with `ada@example.com` / `password123` and send `12.34` EUR to
`bob`. Sign in as Bob with `bob@example.com` / `password123` to see the received
payment. The reset endpoint is for the local test fixture; the sample passwords
are not production credentials.

From the official harness checkout, run `python -m harness check <repo> --track
pocketful` to inspect submission structure, then `python -m harness run --track
pocketful --repo <repo> --stage 4 --out <report-dir>` to run the stage suites.

## Original demo

The original demo has a Python web UI, an HTTP API, and a SQLite double-entry
ledger. Balances are demo data, not real money.

The central requirement is that transfers conserve money and cannot be applied
twice when requests race or are retried. Amounts are stored as integer minor
units. Each transfer writes equal and opposite ledger entries in one transaction.
The wallet persists a transfer's idempotency key before sending it; retry uses
that same key and request body. Browser refreshes issue GETs, not another send.

## Try the hosted demo

[pocketful.getn.space](https://pocketful.getn.space/)

The hosted revision may lag behind this repository. Check the page itself before
assuming a feature in the source has been deployed. The demo is unauthenticated:
an account ID is the capability to use that account. Do not use real funds or
personal financial information.

## Run locally

Python 3.11 or newer is required. The app uses only the standard library.
From the repository root, start the API in one terminal:

```powershell
python -m api --db .\pocketful-local.db --host 127.0.0.1 --port 8001
```

Create the initial account in a second PowerShell terminal:

```powershell
$account = @{ owner_id = 'demo'; currency = 'USD'; account_id = 'acct-demo' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8001/accounts' -ContentType 'application/json' -Body $account
```

Start the web UI in that second terminal:

```powershell
python -m app.web --api http://127.0.0.1:8001 --store .\ui-pending-local.json --account acct-demo --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080/>. The new account starts with a zero balance.
The UI can create more accounts, but ordinary accounts cannot send until they
are funded. For an end-to-end transfer fixture, see `tests/test_web_accounts.py`.
Keep `pocketful-local.db` and `ui-pending-local.json` together between restarts:
the pending file holds keys needed to retry uncertain sends safely.

## Verify

On Linux or WSL, run the project's four-phase gate:

```sh
./run_gate.sh
```

It runs the unit tests, byte compilation, lint, and the API contract self-check.
The source of truth for money-path rules is [INVARIANTS.md](INVARIANTS.md).
See [ops/README.md](ops/README.md) for deployment and backup procedures, and
[plan.md](plan.md) for the agents' task and review history.
