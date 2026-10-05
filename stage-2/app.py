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
    held = S.held(user["id"])
    return jsonify(user_id=user["id"], display_name=user["display_name"], handle=user["handle"], balance=user["balance"], total=user["balance"], available=user["balance"] - held, held=held, currency=S.currency, minor_units=S.minor_units)

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
body{margin:0;background:#f6f7f9;color:#17202a;font-family:Arial,sans-serif}.nav{display:flex;gap:12px;align-items:center;background:#fff;border-bottom:1px solid #d9dee7;padding:12px 16px}.wrap{max-width:1040px;margin:auto;padding:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}.card{background:#fff;border:1px solid #d9dee7;border-radius:8px;padding:16px;margin-bottom:14px}input,select,button{font:inherit;box-sizing:border-box;width:100%;padding:10px;margin:5px 0}button{background:#087568;color:white;border:0;border-radius:6px}.money{font-size:30px;font-weight:700}.muted{color:#667085}.err{color:#b42318}
</style></head><body><div id="app"></div><script>
let token=localStorage.token||'',me=null,payDraft={to_handle:'',amount:'',note:'',visibility:'public'};const q=s=>document.querySelector(s);function fmt(v){let m=me?.minor_units??2,c=me?.currency||'EUR';return (m?(v/10**m).toFixed(m):String(v))+' '+c}function minor(s){let m=me?.minor_units??2;if(!/^\d+(\.\d+)?$/.test(s))throw Error('bad amount');let [a,b='']=s.split('.');if(b.length>m)throw Error('bad amount');return Number(a)*10**m+Number((b+'0'.repeat(m)).slice(0,m))}function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function idem(x){return btoa(JSON.stringify(x)).replace(/[^a-z0-9]/gi,'').slice(0,40)||'k'}
async function api(p,o={}){o.headers={...(o.headers||{}),'Content-Type':'application/json'};if(token)o.headers.Authorization='Bearer '+token;let r=await fetch(p,o);if(r.status==204)return null;let j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error?.code||'error');return j}
async function boot(){if(token)try{me=await api('/me')}catch{token='';localStorage.removeItem('token')}render()}
function nav(){return `<div class=nav><b>Pocketful</b><a href="/">Wallet</a><a href="/requests">Requests</a><a href="/split">Split</a><a href="/authorizations">Holds</a><span style="flex:1"></span>${me?`<span data-testid=current-user>${me.display_name}</span><span data-testid=current-handle>${me.handle}</span><button data-testid=logout-button onclick="localStorage.removeItem('token');token='';me=null;render()">Logout</button>`:''}</div>`}
async function home(){let act=await api('/activity').catch(()=>({payments:[]}));return `<div class=grid><section class=card><div class=muted>Available</div><div class=money data-testid=wallet-available data-amount="${me.available}">${fmt(me.available)}</div><div data-testid=wallet-balance data-amount="${me.balance}">${fmt(me.balance)}</div>${me.held?`<div data-testid=wallet-held data-amount="${me.held}">${fmt(me.held)}</div>`:''}<button data-testid=wallet-refresh onclick=boot()>Refresh</button></section><section class=card><h2>Pay</h2><input data-testid=pay-handle value="${esc(payDraft.to_handle)}"><input data-testid=pay-amount value="${esc(payDraft.amount)}"><input data-testid=pay-note value="${esc(payDraft.note)}"><select data-testid=pay-visibility><option value=public ${payDraft.visibility=='public'?'selected':''}>public</option><option value=private ${payDraft.visibility=='private'?'selected':''}>private</option></select><button data-testid=pay-submit onclick=pay()>Pay</button><div id=paymsg></div></section><section class=card><h2>Request</h2><input data-testid=request-handle><input data-testid=request-amount><input data-testid=request-note><button data-testid=request-submit onclick=req()>Request</button><div id=reqmsg></div></section><section class=card><h2>Authorize</h2><input data-testid=authorize-handle><input data-testid=authorize-amount><input data-testid=authorize-note><select data-testid=authorize-visibility><option value=public>public</option><option value=private>private</option></select><button data-testid=authorize-submit onclick=authz()>Authorize</button><div id=authmsg></div></section></div>${feed(act.payments)}`}
function feed(ps){return ps.length?`<div data-testid=activity-list class=card>${ps.map(p=>`<div data-testid="activity-item-${p.payment_id}" data-visibility="${p.visibility}"><span data-testid="activity-amount-${p.payment_id}">${fmt(p.amount)}</span> <span data-testid="activity-parties-${p.payment_id}">${p.from_handle} ${p.to_handle}</span><div data-testid="activity-note-${p.payment_id}">${p.note}</div></div>`).join('')}</div>`:'<div data-testid=empty-activity class=card>No activity</div>'}
async function pay(){try{payDraft={to_handle:q('[data-testid=pay-handle]').value,amount:q('[data-testid=pay-amount]').value,note:q('[data-testid=pay-note]').value,visibility:q('[data-testid=pay-visibility]').value};let d={to_handle:payDraft.to_handle,amount:minor(payDraft.amount),note:payDraft.note,visibility:payDraft.visibility};await api('/payments',{method:'POST',headers:{'Idempotency-Key':idem(d)},body:JSON.stringify(d)});me=await api('/me');render()}catch(e){q('#paymsg').innerHTML=`<div data-testid=pay-error class=err>${e.message}</div>`}}
async function req(){try{let d={payer_handle:q('[data-testid=request-handle]').value,amount:minor(q('[data-testid=request-amount]').value),note:q('[data-testid=request-note]').value};await api('/requests',{method:'POST',headers:{'Idempotency-Key':idem(d)},body:JSON.stringify(d)});await boot()}catch(e){q('#reqmsg').innerHTML=`<div data-testid=request-error class=err>${e.message}</div>`}}
async function authz(){try{let d={to_handle:q('[data-testid=authorize-handle]').value,amount:minor(q('[data-testid=authorize-amount]').value),note:q('[data-testid=authorize-note]').value,visibility:q('[data-testid=authorize-visibility]').value};await api('/authorizations',{method:'POST',headers:{'Idempotency-Key':idem(d)},body:JSON.stringify(d)});await boot()}catch(e){q('#authmsg').innerHTML=`<div data-testid=authorize-error class=err>${e.message}</div>`}}
async function reqPage(){let r=await api('/requests').catch(()=>({requests:[]}));let row=x=>`<div data-testid="request-item-${x.request_id}" data-status="${x.status}"><span data-testid="request-amount-${x.request_id}">${fmt(x.amount)}</span>${x.status=='pending'&&x.payer_id==me.user_id?`<button data-testid="request-pay-${x.request_id}" onclick="payReq('${x.request_id}')">Pay</button><button data-testid="request-decline-${x.request_id}" onclick="mutReq('${x.request_id}','decline')">Decline</button>`:''}${x.status=='pending'&&x.requester_id==me.user_id?`<button data-testid="request-cancel-${x.request_id}" onclick="mutReq('${x.request_id}','cancel')">Cancel</button>`:''}</div>`;return `<div id=reqerr></div><section class=card><div data-testid=incoming-list>${r.requests.filter(x=>x.payer_id==me.user_id).map(row).join('')}</div><div data-testid=outgoing-list>${r.requests.filter(x=>x.requester_id==me.user_id).map(row).join('')}</div>${r.requests.length?'':'<div data-testid=empty-requests>No requests</div>'}</section>`}
async function payReq(id){try{await api('/requests/'+id+'/pay',{method:'POST',headers:{'Idempotency-Key':'pay'+id},body:'{}'});await boot()}catch(e){q('#reqerr').innerHTML=`<div data-testid=request-error class=err>${e.message}</div>`}}async function mutReq(id,a){try{await api('/requests/'+id+'/'+a,{method:'POST',body:'{}'});await boot()}catch(e){q('#reqerr').innerHTML=`<div data-testid=request-error class=err>${e.message}</div>`}}
function split(){return `<section class=card><input data-testid=split-amount oninput=preview()><input data-testid=split-handles oninput=preview()><input data-testid=split-note><button data-testid=split-submit onclick=doSplit()>Split</button><div data-testid=split-preview></div><div id=spliterr></div></section>`}function preview(){try{let a=minor(q('[data-testid=split-amount]').value),hs=q('[data-testid=split-handles]').value.split(',').map(x=>x.trim()).filter(Boolean),b=Math.floor(a/hs.length),r=a%hs.length;q('[data-testid=split-preview]').innerHTML=hs.map((h,i)=>`<div data-testid="split-share-${h}">${fmt(b+(i<r?1:0))}</div>`).join('')}catch{}}
async function doSplit(){try{let d={amount:minor(q('[data-testid=split-amount]').value),participant_handles:q('[data-testid=split-handles]').value.split(',').map(x=>x.trim()).filter(Boolean),note:q('[data-testid=split-note]').value};await api('/splits',{method:'POST',headers:{'Idempotency-Key':idem(d)},body:JSON.stringify(d)});await boot()}catch(e){q('#spliterr').innerHTML=`<div data-testid=split-error class=err>${e.message}</div>`}}
async function authPage(){let a=await api('/authorizations').catch(()=>({authorizations:[]}));return `<div id=autherr></div><section class=card><div data-testid=authorization-list>${a.authorizations.map(x=>`<div data-testid="authorization-item-${x.authorization_id}" data-status="${x.status}"><span data-testid="authorization-amount-${x.authorization_id}">${fmt(x.amount)}</span>${x.status=='captured'?`<span data-testid="authorization-captured-${x.authorization_id}">${fmt(x.captured_amount)}</span>`:''}<span data-testid="authorization-expires-${x.authorization_id}">${x.expires_at}</span>${x.status=='open'&&x.to_user_id==me.user_id?`<input data-testid="authorization-capture-amount-${x.authorization_id}" value="${(x.remaining_amount/10**me.minor_units).toFixed(me.minor_units)}"><button data-testid="authorization-capture-${x.authorization_id}" onclick="cap('${x.authorization_id}')">Capture</button>`:''}${x.status=='open'&&x.from_user_id==me.user_id?`<button data-testid="authorization-void-${x.authorization_id}" onclick="vvoid('${x.authorization_id}')">Void</button>`:''}</div>`).join('')}</div>${a.authorizations.length?'':'<div data-testid=empty-authorizations>No authorizations</div>'}</section>`}
async function cap(id){try{let d={amount:minor(q(`[data-testid=authorization-capture-amount-${id}]`).value)};await api('/authorizations/'+id+'/capture',{method:'POST',headers:{'Idempotency-Key':idem(d)+id},body:JSON.stringify(d)});await boot()}catch(e){q('#autherr').innerHTML=`<div data-testid=authorization-error class=err>${e.message}</div>`}}async function vvoid(id){try{await api('/authorizations/'+id+'/void',{method:'POST',body:'{}'});await boot()}catch(e){q('#autherr').innerHTML=`<div data-testid=authorization-error class=err>${e.message}</div>`}}
function auth(k){return `<section class=card><input data-testid=${k}-email><input data-testid=${k}-password type=password>${k=='signup'?'<input data-testid=signup-display-name>':''}<button data-testid=${k}-submit onclick="doAuth('${k}')">${k}</button><div id=autherr></div></section>`}async function doAuth(k){try{let d={email:q(`[data-testid=${k}-email]`).value,password:q(`[data-testid=${k}-password]`).value};if(k=='signup')d.display_name=q('[data-testid=signup-display-name]').value;let r=await api('/auth/'+k,{method:'POST',body:JSON.stringify(d)});token=r.token;localStorage.token=token;await boot()}catch(e){q('#autherr').innerHTML=`<div data-testid=auth-error class=err>${e.message}</div>`}}
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
