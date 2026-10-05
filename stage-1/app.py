import copy, json, os, secrets, threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
lock = threading.RLock()

def ts():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def error(status, code):
    return jsonify(error={"code": code, "message": code.replace("_", " ")}), status

def parse_body():
    try:
        data = request.get_json(force=True)
    except Exception:
        return None, error(400, "malformed_request")
    if not isinstance(data, dict):
        return None, error(400, "malformed_request")
    return data, None

def canon(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"))

def normalize_idempotency_value(value):
    if isinstance(value, dict) and isinstance(value.get("sig"), list):
        value = dict(value)
        value["sig"] = tuple(value["sig"])
    return value

def as_amount(value, minimum=1):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValueError
    value = int(value)
    if value < minimum or value > 1000000000:
        raise ValueError
    return value

def valid_note(value):
    return isinstance(value, str) and len(value) <= 200

class State:
    def __init__(self):
        self.load_fixture({"currency": "EUR", "minor_units": 2, "users": []})

    def load_fixture(self, fixture):
        self.currency = fixture.get("currency", "EUR")
        self.minor_units = int(fixture.get("minor_units", 2))
        self.users = {}
        self.email_to_user = {}
        self.handle_to_user = {}
        self.tokens = {}
        self.payments = {}
        self.requests = {}
        self.idempotency = {}
        self.settlement_operator_ids = set(fixture.get("settlement_operator_ids", []) or [])
        self.next_ids = {"u": 1, "p": 1, "rq": 1, "sp": 1, "st": 1}
        for user in fixture.get("users", []) or []:
            balance = int(user.get("balance", 0))
            if balance < 0:
                raise ValueError
            row = {
                "id": user["id"],
                "email": user["email"].lower(),
                "display_name": user.get("display_name", ""),
                "handle": user["handle"],
                "balance": balance,
                "password_hash": generate_password_hash(user.get("password", "")),
            }
            self.users[row["id"]] = row
            self.email_to_user[row["email"]] = row["id"]
            self.handle_to_user[row["handle"]] = row["id"]
        for payment in fixture.get("payments", []) or []:
            row = dict(payment)
            row.setdefault("note", "")
            row.setdefault("visibility", "public")
            row.setdefault("request_id", None)
            row.setdefault("settlement_id", None)
            row.setdefault("created_at", ts())
            self.payments[row["id"]] = row
        for req in fixture.get("requests", []) or []:
            row = dict(req)
            row.setdefault("payment_id", None)
            row.setdefault("created_at", ts())
            self.requests[row["id"]] = row

    def nid(self, prefix):
        value = self.next_ids[prefix]
        self.next_ids[prefix] += 1
        return f"{prefix}_{value}"

S = State()

def current_user():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return S.users.get(S.tokens.get(auth[7:]))

def require_user():
    user = current_user()
    if not user:
        return None, error(401, "unauthenticated")
    return user, None

def payment_json(payment):
    sender = S.users[payment["from_user_id"]]
    receiver = S.users[payment["to_user_id"]]
    return {
        "payment_id": payment["id"],
        "from_user_id": sender["id"],
        "from_handle": sender["handle"],
        "to_user_id": receiver["id"],
        "to_handle": receiver["handle"],
        "amount": payment["amount"],
        "currency": S.currency,
        "note": payment.get("note", ""),
        "visibility": payment.get("visibility", "public"),
        "request_id": payment.get("request_id"),
        "settlement_id": payment.get("settlement_id"),
        "created_at": payment["created_at"],
    }

def request_json(row):
    requester = S.users[row["requester_id"]]
    payer = S.users[row["payer_id"]]
    return {
        "request_id": row["id"],
        "requester_id": requester["id"],
        "requester_handle": requester["handle"],
        "payer_id": payer["id"],
        "payer_handle": payer["handle"],
        "amount": row["amount"],
        "currency": S.currency,
        "note": row.get("note", ""),
        "status": row["status"],
        "payment_id": row.get("payment_id"),
        "created_at": row["created_at"],
    }

def idempotency_check(user, data):
    key = request.headers.get("Idempotency-Key", "")
    if not key:
        return None, error(400, "missing_idempotency_key"), None
    if len(key) > 255:
        return None, error(422, "validation_failed"), None
    sig = (request.method, request.path, canon(data))
    scope = (user["id"], key, request.method, request.path)
    prior = S.idempotency.get(scope)
    if prior:
        if prior["sig"] != sig:
            return None, error(409, "idempotency_key_reuse"), None
        return prior["response"], None, key
    return None, None, key

def save_idempotency(user, key, data, response):
    S.idempotency[(user["id"], key, request.method, request.path)] = {
        "sig": (request.method, request.path, canon(data)),
        "response": copy.deepcopy(response),
    }

def make_payment(from_id, to_id, amount, note, visibility, request_id=None, settlement_id=None, created_at=None):
    pid = S.nid("p")
    row = {
        "id": pid,
        "from_user_id": from_id,
        "to_user_id": to_id,
        "amount": amount,
        "note": note,
        "visibility": visibility,
        "request_id": request_id,
        "settlement_id": settlement_id,
        "created_at": created_at or ts(),
    }
    S.users[from_id]["balance"] -= amount
    S.users[to_id]["balance"] += amount
    S.payments[pid] = row
    return row

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.post("/_test/reset")
def reset():
    data, problem = parse_body()
    if problem:
        return problem
    with lock:
        try:
            new_state = State()
            new_state.load_fixture(data)
        except Exception:
            return error(422, "validation_failed")
        globals()["S"] = new_state
    return "", 204

@app.get("/_test/export")
def export_state():
    with lock:
        state = copy.deepcopy(S.__dict__)
        state["settlement_operator_ids"] = list(state.get("settlement_operator_ids", []))
        state["idempotency"] = [
            {"key": list(k), "value": v}
            for k, v in state.get("idempotency", {}).items()
        ]
        return jsonify(track="pocketful", format_version=1, state=state)

@app.post("/_test/import")
def import_state():
    data, problem = parse_body()
    if problem:
        return problem
    if data.get("track") != "pocketful" or data.get("format_version") != 1 or not isinstance(data.get("state"), dict):
        return error(422, "validation_failed")
    with lock:
        state = State()
        state.__dict__.update(copy.deepcopy(data["state"]))
        state.settlement_operator_ids = set(state.settlement_operator_ids)
        if isinstance(state.idempotency, list):
            state.idempotency = {tuple(row["key"]): normalize_idempotency_value(row["value"]) for row in state.idempotency}
        globals()["S"] = state
    return "", 204

@app.post("/auth/signup")
def signup():
    data, problem = parse_body()
    if problem:
        return problem
    email = data.get("email")
    password = data.get("password")
    display_name = data.get("display_name")
    if not isinstance(email, str) or not isinstance(password, str) or not isinstance(display_name, str):
        return error(400, "malformed_request")
    if "@" not in email or email.startswith("@") or email.endswith("@") or len(password) < 8:
        return error(422, "validation_failed")
    with lock:
        if email.lower() in S.email_to_user:
            return error(409, "email_taken")
        handle = "".join(ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in email.split("@")[0].lower())[:20]
        if not handle or handle in S.handle_to_user:
            return error(409, "handle_taken")
        uid = S.nid("u")
        token = secrets.token_urlsafe(24)
        row = {"id": uid, "email": email.lower(), "display_name": display_name, "handle": handle, "balance": 0, "password_hash": generate_password_hash(password)}
        S.users[uid] = row
        S.email_to_user[row["email"]] = uid
        S.handle_to_user[handle] = uid
        S.tokens[token] = uid
        return jsonify(user_id=uid, display_name=display_name, token=token), 201

@app.post("/auth/login")
def login():
    data, problem = parse_body()
    if problem:
        return problem
    email = data.get("email")
    password = data.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return error(400, "malformed_request")
    with lock:
        user = S.users.get(S.email_to_user.get(email.lower()))
        if not user or not check_password_hash(user["password_hash"], password):
            return error(401, "unauthenticated")
        token = secrets.token_urlsafe(24)
        S.tokens[token] = user["id"]
        return jsonify(user_id=user["id"], display_name=user["display_name"], token=token)

@app.get("/me")
def me():
    user, problem = require_user()
    if problem:
        return problem
    return jsonify(user_id=user["id"], display_name=user["display_name"], handle=user["handle"], balance=user["balance"], currency=S.currency, minor_units=S.minor_units)

def pay_fields(data, handle_name):
    try:
        amount = as_amount(data.get("amount"))
    except Exception:
        return None, None, None, None, error(422, "validation_failed")
    note = data.get("note", "")
    visibility = data.get("visibility", "public")
    handle = data.get(handle_name)
    if not isinstance(handle, str) or not valid_note(note) or visibility not in ("public", "private"):
        return None, None, None, None, error(422, "validation_failed")
    return handle, amount, note, visibility, None

@app.post("/payments")
def create_payment():
    data, problem = parse_body()
    if problem:
        return problem
    user, problem = require_user()
    if problem:
        return problem
    with lock:
        replay, problem, key = idempotency_check(user, data)
        if problem:
            return problem
        if replay is not None:
            return jsonify(replay), 200
        handle, amount, note, visibility, problem = pay_fields(data, "to_handle")
        if problem:
            return problem
        receiver = S.users.get(S.handle_to_user.get(handle))
        if not receiver:
            return error(404, "not_found")
        if receiver["id"] == user["id"]:
            return error(422, "self_payment")
        if user["balance"] < amount:
            return error(409, "insufficient_funds")
        response = payment_json(make_payment(user["id"], receiver["id"], amount, note, visibility))
        save_idempotency(user, key, data, response)
        return jsonify(response), 201

@app.post("/requests")
def create_request():
    data, problem = parse_body()
    if problem:
        return problem
    user, problem = require_user()
    if problem:
        return problem
    with lock:
        replay, problem, key = idempotency_check(user, data)
        if problem:
            return problem
        if replay is not None:
            return jsonify(replay), 200
        handle, amount, note, _visibility, problem = pay_fields({**data, "visibility": "public"}, "payer_handle")
        if problem:
            return problem
        payer = S.users.get(S.handle_to_user.get(handle))
        if not payer:
            return error(404, "not_found")
        if payer["id"] == user["id"]:
            return error(422, "self_request")
        rid = S.nid("rq")
        row = {"id": rid, "requester_id": user["id"], "payer_id": payer["id"], "amount": amount, "note": note, "status": "pending", "payment_id": None, "created_at": ts()}
        S.requests[rid] = row
        response = request_json(row)
        save_idempotency(user, key, data, response)
        return jsonify(response), 201

@app.post("/requests/<rid>/pay")
def pay_request(rid):
    data, problem = parse_body()
    if problem:
        return problem
    user, problem = require_user()
    if problem:
        return problem
    with lock:
        replay, problem, key = idempotency_check(user, data)
        if problem:
            return problem
        if replay is not None:
            return jsonify(replay), 200
        row = S.requests.get(rid)
        if not row:
            return error(404, "not_found")
        if row["payer_id"] != user["id"]:
            return error(403, "forbidden")
        if row["status"] != "pending":
            return error(409, "request_not_pending")
        visibility = data.get("visibility", "public")
        if visibility not in ("public", "private"):
            return error(422, "validation_failed")
        if user["balance"] < row["amount"]:
            return error(409, "insufficient_funds")
        payment = make_payment(user["id"], row["requester_id"], row["amount"], row.get("note", ""), visibility, request_id=rid)
        row["status"] = "paid"
        row["payment_id"] = payment["id"]
        response = payment_json(payment)
        save_idempotency(user, key, data, response)
        return jsonify(response), 201

def request_transition(rid, owner_field, final_status):
    user, problem = require_user()
    if problem:
        return problem
    with lock:
        row = S.requests.get(rid)
        if not row:
            return error(404, "not_found")
        if row[owner_field] != user["id"]:
            return error(403, "forbidden")
        if row["status"] == final_status:
            return jsonify(request_json(row))
        if row["status"] != "pending":
            return error(409, "request_not_pending")
        row["status"] = final_status
        return jsonify(request_json(row))

@app.post("/requests/<rid>/decline")
def decline_request(rid):
    return request_transition(rid, "payer_id", "declined")

@app.post("/requests/<rid>/cancel")
def cancel_request(rid):
    return request_transition(rid, "requester_id", "cancelled")

def page_args():
    limit_text = request.args.get("limit", "50")
    offset_text = request.args.get("offset", "0")
    if not limit_text.isdecimal() or not offset_text.isdecimal():
        raise ValueError
    limit = int(limit_text)
    offset = int(offset_text)
    if limit < 1 or limit > 200 or offset < 0:
        raise ValueError
    return limit, offset

@app.get("/requests")
def list_requests():
    user, problem = require_user()
    if problem:
        return problem
    try:
        limit, offset = page_args()
    except ValueError:
        return error(422, "validation_failed")
    direction = request.args.get("direction")
    status = request.args.get("status")
    if direction not in (None, "incoming", "outgoing") or status not in (None, "pending", "paid", "declined", "cancelled"):
        return error(422, "validation_failed")
    rows = [r for r in S.requests.values() if user["id"] in (r["requester_id"], r["payer_id"])]
    if direction == "incoming":
        rows = [r for r in rows if r["payer_id"] == user["id"]]
    if direction == "outgoing":
        rows = [r for r in rows if r["requester_id"] == user["id"]]
    if status:
        rows = [r for r in rows if r["status"] == status]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return jsonify(requests=[request_json(r) for r in rows[offset:offset + limit]], has_more=offset + limit < len(rows))

@app.post("/splits")
def create_split():
    data, problem = parse_body()
    if problem:
        return problem
    user, problem = require_user()
    if problem:
        return problem
    with lock:
        replay, problem, key = idempotency_check(user, data)
        if problem:
            return problem
        if replay is not None:
            return jsonify(replay), 200
        try:
            amount = as_amount(data.get("amount"))
        except Exception:
            return error(422, "validation_failed")
        handles = data.get("participant_handles")
        note = data.get("note", "")
        if not isinstance(handles, list) or not handles or len(set(handles)) != len(handles) or not valid_note(note):
            return error(422, "validation_failed")
        users = []
        for handle in handles:
            if not isinstance(handle, str):
                return error(422, "validation_failed")
            found = S.users.get(S.handle_to_user.get(handle))
            if not found:
                return error(404, "not_found")
            users.append(found)
        base, rem = divmod(amount, len(handles))
        shares = [{"handle": h, "amount": base + (1 if i < rem else 0)} for i, h in enumerate(handles)]
        created = []
        for share, participant in zip(shares, users):
            if participant["id"] == user["id"]:
                continue
            rid = S.nid("rq")
            row = {"id": rid, "requester_id": user["id"], "payer_id": participant["id"], "amount": share["amount"], "note": note, "status": "pending", "payment_id": None, "created_at": ts()}
            S.requests[rid] = row
            created.append(request_json(row))
        response = {"split_id": S.nid("sp"), "amount": amount, "currency": S.currency, "note": note, "shares": shares, "requests": created, "created_at": ts()}
        save_idempotency(user, key, data, response)
        return jsonify(response), 201

@app.get("/activity")
def activity():
    user, problem = require_user()
    if problem:
        return problem
    try:
        limit, offset = page_args()
    except ValueError:
        return error(422, "validation_failed")
    rows = [p for p in S.payments.values() if p.get("visibility") == "public" or user["id"] in (p["from_user_id"], p["to_user_id"])]
    rows.sort(key=lambda p: p["created_at"], reverse=True)
    return jsonify(payments=[payment_json(p) for p in rows[offset:offset + limit]], has_more=offset + limit < len(rows))

@app.post("/settlements")
def settlements():
    data, problem = parse_body()
    if problem:
        return problem
    user, problem = require_user()
    if problem:
        return problem
    if user["id"] not in S.settlement_operator_ids:
        return error(403, "forbidden")
    with lock:
        replay, problem, key = idempotency_check(user, data)
        if problem:
            return problem
        if replay is not None:
            return jsonify(replay), 200
        transfers = data.get("transfers")
        if not isinstance(transfers, list) or not 1 <= len(transfers) <= 32:
            return error(422, "validation_failed")
        parsed = []
        net = {}
        for transfer in transfers:
            if not isinstance(transfer, dict):
                return error(422, "validation_failed")
            try:
                amount = as_amount(transfer.get("amount"))
            except Exception:
                return error(422, "validation_failed")
            note = transfer.get("note", "")
            visibility = transfer.get("visibility", "public")
            if not valid_note(note) or visibility not in ("public", "private"):
                return error(422, "validation_failed")
            sender = S.users.get(S.handle_to_user.get(transfer.get("from_handle")))
            receiver = S.users.get(S.handle_to_user.get(transfer.get("to_handle")))
            if not sender or not receiver:
                return error(404, "not_found")
            if sender["id"] == receiver["id"]:
                return error(422, "self_payment")
            parsed.append((sender, receiver, amount, note, visibility))
            net[sender["id"]] = net.get(sender["id"], 0) - amount
            net[receiver["id"]] = net.get(receiver["id"], 0) + amount
        for uid, delta in net.items():
            if S.users[uid]["balance"] + delta < 0:
                return error(409, "insufficient_funds")
        sid = S.nid("st")
        committed = ts()
        payments = [payment_json(make_payment(a["id"], b["id"], amount, note, visibility, settlement_id=sid, created_at=committed)) for a, b, amount, note, visibility in parsed]
        response = {"settlement_id": sid, "committed_at": committed, "payments": payments}
        save_idempotency(user, key, data, response)
        return jsonify(response), 201

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
