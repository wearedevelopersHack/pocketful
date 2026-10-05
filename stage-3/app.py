import copy, json, os, secrets, threading
from datetime import datetime, timedelta, timezone
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
        self.authorizations = {}
        self.authorization_ttl_seconds = int(fixture.get("authorization_ttl_seconds", 600) or 600)
        self.idempotency = {}
        self.settlement_operator_ids = set(fixture.get("settlement_operator_ids", []) or [])
        self.next_ids = {"u": 1, "p": 1, "rq": 1, "sp": 1, "st": 1, "a": 1}
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
        for auth in fixture.get("authorizations", []) or []:
            row = dict(auth)
            row.setdefault("captured_amount", 0)
            row.setdefault("note", "")
            row.setdefault("visibility", "public")
            row.setdefault("status", "open")
            row.setdefault("payment_id", None)
            row.setdefault("payment_ids", [])
            row.setdefault("created_at", ts())
            row.setdefault("closed_at", None)
            self.authorizations[row["id"]] = row
        for user in self.users.values():
            if self.held(user["id"]) > user["balance"]:
                raise ValueError

    def held(self, user_id):
        total = 0
        now_text = ts()
        for auth in self.authorizations.values():
            if auth["from_user_id"] == user_id and auth["status"] == "open":
                if auth.get("expires_at") <= now_text:
                    auth["status"] = "expired"
                    auth["closed_at"] = auth.get("expires_at")
                    continue
                total += max(0, auth["amount"] - auth.get("captured_amount", 0))
        return total

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
        "authorization_id": payment.get("authorization_id"),
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
    as_of = request.args.get("as_of")
    known_at = request.args.get("known_at")
    for value in (as_of, known_at):
        if value is not None:
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError
            except Exception:
                return error(422, "validation_failed")
    held = S.held(user["id"])
    body = dict(user_id=user["id"], display_name=user["display_name"], handle=user["handle"], balance=user["balance"], total=user["balance"], available=user["balance"] - held, held=held, currency=S.currency, minor_units=S.minor_units)
    body.update({k: v for k, v in {"as_of": as_of, "known_at": known_at}.items() if v is not None})
    return jsonify(body)

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
        if user["balance"] - S.held(user["id"]) < amount:
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
        if user["balance"] - S.held(user["id"]) < row["amount"]:
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
    if "text/html" in request.headers.get("Accept", ""):
        return ui()
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

HTML = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pocketful</title><style>
:root{--bg:#f4f7f6;--panel:#fff;--ink:#17202a;--muted:#667085;--line:#d9e1df;--brand:#087568;--brand-dark:#055e54;--soft:#e8f3f1;--danger:#b42318;--danger-bg:#fff1f0;--ok:#067647;--ok-bg:#ecfdf3}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,Segoe UI,Arial,sans-serif}.nav{position:sticky;top:0;z-index:3;display:flex;gap:10px;align-items:center;background:rgba(255,255,255,.96);border-bottom:1px solid var(--line);padding:10px 16px;box-shadow:0 1px 8px #10241f12}.brand{font-weight:800;font-size:18px;margin-right:8px}.nav a{color:#344054;text-decoration:none;padding:8px 10px;border-radius:6px}.nav a:hover,.nav a:focus-visible{background:var(--soft);color:var(--brand-dark)}.spacer{flex:1}.user{display:flex;gap:8px;align-items:center;color:#344054;font-size:14px}.handle{color:var(--muted)}.wrap{max-width:1120px;margin:auto;padding:22px}.grid{display:grid;grid-template-columns:minmax(260px,.9fr) repeat(3,minmax(230px,1fr));gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:14px;box-shadow:0 8px 24px #10241f0d}.panel-title{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 12px}.panel-title h2,.panel-title h1{margin:0;font-size:18px}.eyebrow{font-size:12px;text-transform:uppercase;color:var(--muted);font-weight:700;letter-spacing:.04em}.money{font-size:34px;line-height:1.1;font-weight:800;margin:8px 0}.balance-row{display:flex;gap:10px;flex-wrap:wrap;color:#344054}.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:5px 9px;background:#fafafa;font-size:13px}.field{display:grid;gap:5px;margin:9px 0}.field label{font-size:13px;font-weight:700;color:#344054}.hint{font-size:12px;color:var(--muted)}input,select,button{font:inherit}input,select{width:100%;padding:10px 11px;border:1px solid #cbd5d2;border-radius:6px;background:#fff;color:var(--ink)}input:focus,select:focus,button:focus-visible{outline:3px solid #9ee6dc;outline-offset:1px;border-color:var(--brand)}button{width:auto;padding:9px 12px;border:0;border-radius:6px;background:var(--brand);color:white;font-weight:700;cursor:pointer}button:hover{background:var(--brand-dark)}button.secondary{background:#eef4f3;color:#0f5149}button.danger{background:#fff1f0;color:var(--danger);border:1px solid #f6c7c2}.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.actions button{flex:1}.logout{padding:7px 10px;background:#eef4f3;color:#0f5149}.err,.ok{margin-top:10px;border-radius:6px;padding:9px 10px;font-size:14px}.err{color:var(--danger);background:var(--danger-bg);border:1px solid #f4c7c3}.ok{color:var(--ok);background:var(--ok-bg);border:1px solid #b7e4c7}.list-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}.activity,.request-row,.auth-row,.split-row{display:grid;gap:5px;border-top:1px solid var(--line);padding:12px 0}.activity:first-child,.request-row:first-child,.auth-row:first-child,.split-row:first-child{border-top:0}.split-row{grid-template-columns:1fr auto;align-items:center}.split-share{font-weight:800}.row-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.amount{font-weight:800}.muted{color:var(--muted)}.note{color:#344054}.empty{color:var(--muted);text-align:center;padding:20px}.auth-card{max-width:420px;margin:8vh auto}.page-heading{margin:0 0 14px}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media (max-width:980px){.grid{grid-template-columns:1fr 1fr}.grid .wallet{grid-column:1/-1}}@media (max-width:680px){.nav{align-items:flex-start;flex-wrap:wrap}.brand{width:100%}.spacer{display:none}.user{width:100%;justify-content:space-between}.wrap{padding:14px}.grid,.two-col{grid-template-columns:1fr}.actions button,.logout{flex:1 1 auto}.money{font-size:30px}}
</style></head><body><div id="app"></div><script>
let token=localStorage.token||'',me=null,payDraft={to_handle:'',amount:'',note:'',visibility:'public'},paySig='',payKey='';const q=s=>document.querySelector(s);function fmt(v){let m=me?.minor_units??2,c=me?.currency||'EUR';return (m?(v/10**m).toFixed(m):String(v))+' '+c}function minor(s){let m=me?.minor_units??2;if(!/^\d+(\.\d+)?$/.test(s))throw Error('bad amount');let [a,b='']=s.split('.');if(b.length>m)throw Error('bad amount');return Number(a)*10**m+Number((b+'0'.repeat(m)).slice(0,m))}function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function idem(x){return btoa(JSON.stringify(x)).replace(/[^a-z0-9]/gi,'').slice(0,40)||'k'}function field(label,test,value='',type='text',hint='',extra=''){return `<div class=field><label>${label}</label><input data-testid=${test} type=${type} value="${esc(value)}" ${extra}>${hint?`<div class=hint>${hint}</div>`:''}</div>`}function status(id,msg,ok=false){return `<div data-testid=${id} class=${ok?'ok':'err'}>${esc(msg)}</div>`}
async function api(p,o={}){o.headers={...(o.headers||{}),'Content-Type':'application/json'};if(token)o.headers.Authorization='Bearer '+token;let r=await fetch(p,o);if(r.status==204)return null;let j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error?.code||'error');return j}
async function boot(){if(token)try{me=await api('/me')}catch{token='';localStorage.removeItem('token')}render()}
function nav(){return `<div class=nav><div class=brand>Pocketful</div><a href="/">Wallet</a><a href="/requests">Requests</a><a href="/split">Split</a><a href="/authorizations">Holds</a><span class=spacer></span>${me?`<div class=user><span data-testid=current-user>${esc(me.display_name)}</span><span class=handle data-testid=current-handle>${esc(me.handle)}</span><button class=logout data-testid=logout-button onclick="localStorage.removeItem('token');token='';me=null;render()">Logout</button></div>`:''}</div>`}
async function home(){let act=await api('/activity').catch(()=>({payments:[]}));return `<div class=grid><section class="card wallet"><div class=eyebrow>Available</div><div class=money data-testid=wallet-available data-amount="${me.available}">${fmt(me.available)}</div><div class=balance-row><span class=pill data-testid=wallet-balance data-amount="${me.balance}">${fmt(me.balance)}</span>${me.held?`<span class=pill data-testid=wallet-held data-amount="${me.held}">Held ${fmt(me.held)}</span>`:''}</div><div class=actions style="margin-top:14px"><button class=secondary data-testid=wallet-refresh onclick=boot()>Refresh</button></div></section><section class=card><div class=panel-title><h2>Pay</h2><span class=eyebrow>Send</span></div>${field('Recipient handle','pay-handle',payDraft.to_handle,'text','Example: alex')}${field(`Amount (${me.currency})`,'pay-amount',payDraft.amount,'text','Use major units, e.g. 12.50')}${field('Note','pay-note',payDraft.note)}<div class=field><label>Visibility</label><select data-testid=pay-visibility><option value=public ${payDraft.visibility=='public'?'selected':''}>Public</option><option value=private ${payDraft.visibility=='private'?'selected':''}>Private</option></select></div><button data-testid=pay-submit onclick=pay()>Pay</button><div id=paymsg></div></section><section class=card><div class=panel-title><h2>Request</h2><span class=eyebrow>Collect</span></div>${field('Payer handle','request-handle','','text','Who should pay you')}${field(`Amount (${me.currency})`,'request-amount','','text','Use major units')}${field('Note','request-note')}<button data-testid=request-submit onclick=req()>Request</button><div id=reqmsg></div></section><section class=card><div class=panel-title><h2>Hold</h2><span class=eyebrow>Authorize</span></div>${field('Recipient handle','authorize-handle','','text','Who may capture this hold')}${field(`Amount (${me.currency})`,'authorize-amount','','text','Use major units')}${field('Note','authorize-note')}<div class=field><label>Visibility</label><select data-testid=authorize-visibility><option value=public>Public</option><option value=private>Private</option></select></div><button data-testid=authorize-submit onclick=authz()>Authorize</button><div id=authmsg></div></section></div>${feed(act.payments)}`}
function feed(ps){return ps.length?`<section data-testid=activity-list class=card><div class=list-title><h2 class=page-heading>Activity</h2><span class=muted>${ps.length} shown</span></div>${ps.map(p=>`<div class=activity data-testid="activity-item-${p.payment_id}" data-visibility="${p.visibility}"><div class=row-head><span class=amount data-testid="activity-amount-${p.payment_id}">${fmt(p.amount)}</span><span class=pill>${p.visibility}</span></div><span data-testid="activity-parties-${p.payment_id}">${esc(p.from_handle)} to ${esc(p.to_handle)}</span><div class=note data-testid="activity-note-${p.payment_id}">${esc(p.note||'')}</div></div>`).join('')}</section>`:'<section data-testid=empty-activity class="card empty">No activity yet</section>'}
async function pay(){try{payDraft={to_handle:q('[data-testid=pay-handle]').value,amount:q('[data-testid=pay-amount]').value,note:q('[data-testid=pay-note]').value,visibility:q('[data-testid=pay-visibility]').value};let d={to_handle:payDraft.to_handle,amount:minor(payDraft.amount),note:payDraft.note,visibility:payDraft.visibility},sig=JSON.stringify(d);if(sig!=paySig){paySig=sig;payKey=idem(d)}await api('/payments',{method:'POST',headers:{'Idempotency-Key':payKey},body:sig});me=await api('/me');render()}catch(e){q('#paymsg').innerHTML=status('pay-error',e.message)}}
async function req(){try{let d={payer_handle:q('[data-testid=request-handle]').value,amount:minor(q('[data-testid=request-amount]').value),note:q('[data-testid=request-note]').value};await api('/requests',{method:'POST',headers:{'Idempotency-Key':idem(d)},body:JSON.stringify(d)});await boot()}catch(e){q('#reqmsg').innerHTML=status('request-error',e.message)}}
async function authz(){try{let d={to_handle:q('[data-testid=authorize-handle]').value,amount:minor(q('[data-testid=authorize-amount]').value),note:q('[data-testid=authorize-note]').value,visibility:q('[data-testid=authorize-visibility]').value};await api('/authorizations',{method:'POST',headers:{'Idempotency-Key':idem(d)},body:JSON.stringify(d)});await boot()}catch(e){q('#authmsg').innerHTML=status('authorize-error',e.message)}}
async function reqPage(){let r=await api('/requests').catch(()=>({requests:[]}));let row=x=>`<div class=request-row data-testid="request-item-${x.request_id}" data-status="${x.status}"><div class=row-head><span class=amount data-testid="request-amount-${x.request_id}">${fmt(x.amount)}</span><span class=pill>${x.status}</span></div><div class=muted>${esc(x.requester_handle)} requested from ${esc(x.payer_handle)}</div>${x.note?`<div class=note>${esc(x.note)}</div>`:''}<div class=actions>${x.status=='pending'&&x.payer_id==me.user_id?`<button data-testid="request-pay-${x.request_id}" onclick="payReq('${x.request_id}')">Pay</button><button class=secondary data-testid="request-decline-${x.request_id}" onclick="mutReq('${x.request_id}','decline')">Decline</button>`:''}${x.status=='pending'&&x.requester_id==me.user_id?`<button class=secondary data-testid="request-cancel-${x.request_id}" onclick="mutReq('${x.request_id}','cancel')">Cancel</button>`:''}</div></div>`;return `<div id=reqerr></div><section class=card><h1 class=page-heading>Requests</h1><div class=two-col><div><div class=eyebrow>Incoming</div><div data-testid=incoming-list>${r.requests.filter(x=>x.payer_id==me.user_id).map(row).join('')||'<div class=empty>No incoming requests</div>'}</div></div><div><div class=eyebrow>Outgoing</div><div data-testid=outgoing-list>${r.requests.filter(x=>x.requester_id==me.user_id).map(row).join('')||'<div class=empty>No outgoing requests</div>'}</div></div></div>${r.requests.length?'':'<div data-testid=empty-requests class=empty>No requests yet</div>'}</section>`}
async function payReq(id){try{await api('/requests/'+id+'/pay',{method:'POST',headers:{'Idempotency-Key':'pay'+id},body:'{}'});await boot()}catch(e){q('#reqerr').innerHTML=status('request-error',e.message)}}async function mutReq(id,a){try{await api('/requests/'+id+'/'+a,{method:'POST',body:'{}'});await boot()}catch(e){q('#reqerr').innerHTML=status('request-error',e.message)}}
function split(){return `<section class=card><h1 class=page-heading>Split a bill</h1>${field(`Total amount (${me.currency})`,'split-amount','','text','Use major units','oninput=preview()')}${field('Participant handles','split-handles','','text','Comma-separated handles','oninput=preview()')}${field('Note','split-note')}<button data-testid=split-submit onclick=doSplit()>Split</button><div data-testid=split-preview class=card style="margin-top:14px"></div><div id=spliterr></div></section>`}function preview(){try{let a=minor(q('[data-testid=split-amount]').value),hs=q('[data-testid=split-handles]').value.split(',').map(x=>x.trim()).filter(Boolean),b=Math.floor(a/hs.length),r=a%hs.length;q('[data-testid=split-preview]').innerHTML=hs.map((h,i)=>`<div class=split-row><span class=handle>${esc(h)}</span><span class=split-share data-testid="split-share-${h}">${fmt(b+(i<r?1:0))}</span></div>`).join('')||'<div class=empty>Add handles to preview shares</div>'}catch{if(q('[data-testid=split-preview]'))q('[data-testid=split-preview]').innerHTML='<div class=empty>Enter an amount and handles to preview shares</div>'}}
async function doSplit(){try{let d={amount:minor(q('[data-testid=split-amount]').value),participant_handles:q('[data-testid=split-handles]').value.split(',').map(x=>x.trim()).filter(Boolean),note:q('[data-testid=split-note]').value};await api('/splits',{method:'POST',headers:{'Idempotency-Key':idem(d)},body:JSON.stringify(d)});await boot()}catch(e){q('#spliterr').innerHTML=status('split-error',e.message)}}
async function authPage(){let a=await api('/authorizations').catch(()=>({authorizations:[]}));return `<div id=autherr></div><section class=card><h1 class=page-heading>Holds</h1><div data-testid=authorization-list>${a.authorizations.map(x=>`<div class=auth-row data-testid="authorization-item-${x.authorization_id}" data-status="${x.status}"><div class=row-head><span class=amount data-testid="authorization-amount-${x.authorization_id}">${fmt(x.amount)}</span><span class=pill>${x.status}</span></div>${x.status=='captured'?`<div>Captured <span data-testid="authorization-captured-${x.authorization_id}">${fmt(x.captured_amount)}</span></div>`:''}<div class=muted>Expires <span data-testid="authorization-expires-${x.authorization_id}">${x.expires_at}</span></div><div class=muted>${esc(x.from_handle)} to ${esc(x.to_handle)}</div><div class=actions>${x.status=='open'&&x.to_user_id==me.user_id?`<input data-testid="authorization-capture-amount-${x.authorization_id}" value="${(x.remaining_amount/10**me.minor_units).toFixed(me.minor_units)}"><button data-testid="authorization-capture-${x.authorization_id}" onclick="cap('${x.authorization_id}')">Capture</button>`:''}${x.status=='open'&&x.from_user_id==me.user_id?`<button class=danger data-testid="authorization-void-${x.authorization_id}" onclick="vvoid('${x.authorization_id}')">Void</button>`:''}</div></div>`).join('')}</div>${a.authorizations.length?'':'<div data-testid=empty-authorizations class=empty>No holds yet</div>'}</section>`}
async function cap(id){try{let d={amount:minor(q(`[data-testid=authorization-capture-amount-${id}]`).value)};await api('/authorizations/'+id+'/capture',{method:'POST',headers:{'Idempotency-Key':idem(d)+id},body:JSON.stringify(d)});await boot()}catch(e){q('#autherr').innerHTML=status('authorization-error',e.message)}}async function vvoid(id){try{await api('/authorizations/'+id+'/void',{method:'POST',body:'{}'});await boot()}catch(e){q('#autherr').innerHTML=status('authorization-error',e.message)}}
function auth(k){let title=k=='signup'?'Create account':'Sign in';return `<section class="card auth-card"><h1 class=page-heading>${title}</h1>${field('Email address',`${k}-email`,'','email')}${field('Password',`${k}-password`,'','password','At least 8 characters')}${k=='signup'?field('Display name','signup-display-name'):''}<button data-testid=${k}-submit onclick="doAuth('${k}')">${title}</button><div class=hint style="margin-top:12px">${k=='signup'?'<a href="/login">Already have an account?</a>':'<a href="/signup">Create an account</a>'}</div><div id=autherr></div></section>`}async function doAuth(k){try{let d={email:q(`[data-testid=${k}-email]`).value,password:q(`[data-testid=${k}-password]`).value};if(k=='signup')d.display_name=q('[data-testid=signup-display-name]').value;let r=await api('/auth/'+k,{method:'POST',body:JSON.stringify(d)});token=r.token;localStorage.token=token;if(location.pathname=='/login'||location.pathname=='/signup')history.replaceState(null,'','/');await boot()}catch(e){q('#autherr').innerHTML=status('auth-error',e.message)}}
async function render(){let p=location.pathname,c;if(p=='/login')c=auth('login');else if(!me&&p!='/signup')c=auth('login');else if(p=='/signup')c=auth('signup');else if(p=='/requests')c=await reqPage();else if(p=='/split')c=split();else if(p=='/authorizations')c=await authPage();else c=await home();document.getElementById('app').innerHTML=nav()+`<main class=wrap>${c}</main>`;if(p=='/split')preview()}boot();
</script></body></html>'''

@app.get("/")
@app.get("/login")
@app.get("/signup")
@app.get("/split")
def ui():
    return HTML

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
            if S.users[uid]["balance"] - S.held(uid) + delta < 0:
                return error(409, "insufficient_funds")
        sid = S.nid("st")
        committed = ts()
        payments = [payment_json(make_payment(a["id"], b["id"], amount, note, visibility, settlement_id=sid, created_at=committed)) for a, b, amount, note, visibility in parsed]
        response = {"settlement_id": sid, "committed_at": committed, "payments": payments}
        save_idempotency(user, key, data, response)
        return jsonify(response), 201

def auth_json(row):
    if row["status"] == "open" and row.get("expires_at") <= ts():
        row["status"] = "expired"
        row["closed_at"] = row.get("expires_at")
    sender = S.users[row["from_user_id"]]
    receiver = S.users[row["to_user_id"]]
    remaining = max(0, row["amount"] - row.get("captured_amount", 0)) if row["status"] == "open" else 0
    return {
        "authorization_id": row["id"],
        "from_user_id": sender["id"],
        "from_handle": sender["handle"],
        "to_user_id": receiver["id"],
        "to_handle": receiver["handle"],
        "amount": row["amount"],
        "captured_amount": row.get("captured_amount", 0),
        "remaining_amount": remaining,
        "currency": S.currency,
        "note": row.get("note", ""),
        "visibility": row.get("visibility", "public"),
        "status": row["status"],
        "expires_at": row["expires_at"],
        "payment_id": row.get("payment_id"),
        "payment_ids": row.get("payment_ids", []),
        "created_at": row["created_at"],
        "closed_at": row.get("closed_at"),
    }

@app.post("/authorizations")
def create_authorization():
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
        if user["balance"] - S.held(user["id"]) < amount:
            return error(409, "insufficient_funds")
        from datetime import timedelta
        created = datetime.now(timezone.utc).replace(microsecond=0)
        aid = S.nid("a")
        row = {"id": aid, "from_user_id": user["id"], "to_user_id": receiver["id"], "amount": amount, "captured_amount": 0, "note": note, "visibility": visibility, "status": "open", "expires_at": (created + timedelta(seconds=S.authorization_ttl_seconds)).isoformat(), "payment_id": None, "payment_ids": [], "created_at": created.isoformat(), "closed_at": None}
        S.authorizations[aid] = row
        response = auth_json(row)
        save_idempotency(user, key, data, response)
        return jsonify(response), 201

@app.get("/authorizations")
def list_authorizations():
    if "text/html" in request.headers.get("Accept", ""):
        return ui()
    user, problem = require_user()
    if problem:
        return problem
    rows = [a for a in S.authorizations.values() if user["id"] in (a["from_user_id"], a["to_user_id"])]
    direction = request.args.get("direction")
    status = request.args.get("status")
    if direction not in (None, "incoming", "outgoing") or status not in (None, "open", "captured", "voided", "expired"):
        return error(422, "validation_failed")
    if direction == "incoming":
        rows = [a for a in rows if a["to_user_id"] == user["id"]]
    if direction == "outgoing":
        rows = [a for a in rows if a["from_user_id"] == user["id"]]
    shaped = [auth_json(a) for a in rows]
    if status:
        shaped = [a for a in shaped if a["status"] == status]
    shaped.sort(key=lambda a: a["created_at"], reverse=True)
    return jsonify(authorizations=shaped, has_more=False)

@app.post("/authorizations/<aid>/capture")
def capture_authorization(aid):
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
        row = S.authorizations.get(aid)
        if not row:
            return error(404, "not_found")
        if row["to_user_id"] != user["id"]:
            return error(403, "forbidden")
        if row["status"] != "open":
            return error(409, "authorization_not_open")
        if row.get("expires_at") <= ts():
            row["status"] = "expired"
            row["closed_at"] = row.get("expires_at")
            return error(409, "authorization_expired")
        remaining = row["amount"] - row.get("captured_amount", 0)
        try:
            amount = as_amount(data.get("amount", remaining))
        except Exception:
            return error(422, "validation_failed")
        if amount > remaining:
            return error(422, "capture_exceeds_authorization")
        payment = make_payment(row["from_user_id"], row["to_user_id"], amount, row.get("note", ""), row.get("visibility", "public"))
        payment["authorization_id"] = aid
        row["captured_amount"] = row.get("captured_amount", 0) + amount
        row["payment_id"] = payment["id"]
        row.setdefault("payment_ids", []).append(payment["id"])
        if data.get("final", True) is not False or row["captured_amount"] >= row["amount"]:
            row["status"] = "captured"
            row["closed_at"] = ts()
        response = payment_json(payment)
        response["authorization_id"] = aid
        save_idempotency(user, key, data, response)
        return jsonify(response), 201

@app.post("/authorizations/<aid>/void")
def void_authorization(aid):
    user, problem = require_user()
    if problem:
        return problem
    with lock:
        row = S.authorizations.get(aid)
        if not row:
            return error(404, "not_found")
        if row["from_user_id"] != user["id"]:
            return error(403, "forbidden")
        if row["status"] == "voided":
            return jsonify(auth_json(row))
        if row["status"] != "open":
            return error(409, "authorization_not_open")
        row["status"] = "voided"
        row["closed_at"] = ts()
        return jsonify(auth_json(row))

def parse_instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except Exception:
        raise ValueError

def after_revisions(*revision_sets):
    recorded = parse_instant(ts())
    priors = [parse_instant(r["recorded_at"]) for revisions in revision_sets for r in revisions]
    if priors:
        latest = max(priors)
        if recorded <= latest:
            recorded = latest + timedelta(seconds=1)
    return recorded.isoformat()

def ensure_revisions(payment):
    revisions = payment.setdefault("revisions", [])
    if not revisions:
        revisions.append({
            "revision": 1,
            "amount": payment["amount"],
            "effective_at": payment["created_at"],
            "recorded_at": payment["created_at"],
            "reason": "",
        })
    return revisions

@app.get("/payments/<pid>/revisions")
def payment_revisions(pid):
    user, problem = require_user()
    if problem:
        return problem
    payment = S.payments.get(pid)
    if not payment or user["id"] not in (payment["from_user_id"], payment["to_user_id"]):
        return error(404, "not_found")
    return jsonify(revisions=ensure_revisions(payment))

@app.post("/payments/<pid>/corrections")
def payment_correction(pid):
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
        payment = S.payments.get(pid)
        if not payment:
            return error(404, "not_found")
        if payment.get("settlement_id") or payment.get("authorization_id"):
            return error(422, "linked_payment_immutable")
        if payment["from_user_id"] != user["id"]:
            return error(403, "forbidden")
        try:
            expected = int(data.get("expected_revision"))
            amount = as_amount(data.get("amount"), minimum=0)
            parse_instant(data.get("effective_at"))
        except Exception:
            return error(422, "validation_failed")
        reason = data.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 200:
            return error(422, "validation_failed")
        revisions = ensure_revisions(payment)
        if expected != revisions[-1]["revision"]:
            return error(409, "stale_revision")
        delta = amount - revisions[-1]["amount"]
        debtor = payment["from_user_id"] if delta > 0 else payment["to_user_id"]
        if delta and S.users[debtor]["balance"] - S.held(debtor) < abs(delta):
            return error(409, "insufficient_funds")
        if delta > 0:
            S.users[payment["from_user_id"]]["balance"] -= delta
            S.users[payment["to_user_id"]]["balance"] += delta
        elif delta < 0:
            S.users[payment["to_user_id"]]["balance"] += delta
            S.users[payment["from_user_id"]]["balance"] -= delta
        revision = {"payment_id": pid, "revision": expected + 1, "amount": amount, "effective_at": data["effective_at"], "recorded_at": after_revisions(revisions), "reason": reason}
        revisions.append({k: revision[k] for k in ("revision", "amount", "effective_at", "recorded_at", "reason")})
        save_idempotency(user, key, data, revision)
        return jsonify(revision), 201

@app.get("/statement")
def statement():
    user, problem = require_user()
    if problem:
        return problem
    try:
        limit, offset = page_args()
        from_arg = request.args.get("from")
        to_arg = request.args.get("to")
        known_at = request.args.get("known_at")
        if from_arg:
            parse_instant(from_arg)
        if to_arg:
            parse_instant(to_arg)
        if known_at:
            parse_instant(known_at)
    except Exception:
        return error(422, "validation_failed")
    rows = [p for p in S.payments.values() if user["id"] in (p["from_user_id"], p["to_user_id"])]
    rows.sort(key=lambda p: (ensure_revisions(p)[-1].get("effective_at", p["created_at"]), p["id"]))
    opening = user["balance"]
    entries = []
    running = opening
    for p in rows:
        rev = ensure_revisions(p)[-1]
        amount = rev["amount"]
        delta = -amount if p["from_user_id"] == user["id"] else amount
        running += delta
        shaped = payment_json({**p, "amount": amount})
        entries.append({"payment": shaped, "delta": delta, "balance_after": running, "revision": rev["revision"], "effective_at": rev["effective_at"], "recorded_at": rev["recorded_at"]})
    body = {"opening_balance": opening, "entries": entries[offset:offset + limit], "closing_balance": running, "has_more": offset + limit < len(entries), "snapshot": secrets.token_urlsafe(12)}
    if known_at:
        body["known_at"] = known_at
    return jsonify(body)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
