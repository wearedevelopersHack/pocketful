import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MAX_AMOUNT = 1000000000
HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password, stored):
    try:
        algo, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    return hmac.compare_digest(hash_password(password, salt).split("$", 2)[2], digest)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset({"currency": "EUR", "minor_units": 2, "users": [], "payments": [], "requests": []})

    def empty(self):
        return {
            "currency": "EUR", "minor_units": 2, "users": {}, "email_to_id": {}, "handle_to_id": {},
            "tokens": {}, "payments": {}, "requests": {}, "splits": {}, "settlements": {},
            "operators": set(), "idem": {}, "seq": {"u": 1, "p": 1, "rq": 1, "sp": 1, "st": 1}
        }

    def reset(self, fixture):
        ns = self.empty()
        currency = fixture.get("currency")
        minor = fixture.get("minor_units")
        if not isinstance(currency, str) or minor not in (0, 2, 3):
            raise ValueError("validation_failed")
        ns["currency"], ns["minor_units"] = currency, minor
        for u in fixture.get("users", []):
            if u.get("balance", -1) < 0:
                raise ValueError("validation_failed")
            uid, email, handle = u.get("id"), u.get("email"), u.get("handle")
            if not all(isinstance(x, str) for x in (uid, email, handle)) or not HANDLE_RE.match(handle):
                raise ValueError("validation_failed")
            user = {
                "id": uid, "email": email, "display_name": u.get("display_name", ""),
                "handle": handle, "balance": int(u.get("balance", 0)),
                "password_hash": hash_password(u.get("password", ""))
            }
            ns["users"][uid] = user
            ns["email_to_id"][email] = uid
            ns["handle_to_id"][handle] = uid
        for uid in fixture.get("settlement_operator_ids", []):
            if uid in ns["users"]:
                ns["operators"].add(uid)
        for p in fixture.get("payments", []):
            pid = p["id"]
            ns["payments"][pid] = {
                "id": pid, "from_user_id": p["from_user_id"], "to_user_id": p["to_user_id"],
                "amount": int(p["amount"]), "note": p.get("note", ""),
                "visibility": p.get("visibility", "public"), "request_id": p.get("request_id"),
                "settlement_id": p.get("settlement_id"), "created_at": p.get("created_at", now())
            }
        for r in fixture.get("requests", []):
            rid = r["id"]
            ns["requests"][rid] = {
                "id": rid, "requester_id": r["requester_id"], "payer_id": r["payer_id"],
                "amount": int(r["amount"]), "note": r.get("note", ""), "status": r.get("status", "pending"),
                "payment_id": r.get("payment_id"), "created_at": r.get("created_at", now())
            }
        self.state = ns

    def nid(self, prefix):
        n = self.state["seq"][prefix]
        self.state["seq"][prefix] += 1
        return f"{prefix}_{n}"

    def validate_import_state(self, st):
        if not isinstance(st, dict):
            raise ValueError("validation_failed")
        required = {
            "currency", "minor_units", "users", "email_to_id", "handle_to_id", "tokens",
            "payments", "requests", "splits", "settlements", "operators", "idem", "seq"
        }
        if set(st.keys()) != required:
            raise ValueError("validation_failed")
        if not isinstance(st["currency"], str) or st["minor_units"] not in (0, 2, 3):
            raise ValueError("validation_failed")
        for name in ("users", "email_to_id", "handle_to_id", "tokens", "payments", "requests", "splits", "settlements", "seq"):
            if not isinstance(st[name], dict):
                raise ValueError("validation_failed")
        if not isinstance(st["operators"], list) or not isinstance(st["idem"], list):
            raise ValueError("validation_failed")
        if set(st["seq"].keys()) != {"u", "p", "rq", "sp", "st"} or any(not isinstance(v, int) or v < 1 for v in st["seq"].values()):
            raise ValueError("validation_failed")

        users = st["users"]
        for uid, u in users.items():
            if not isinstance(uid, str) or not isinstance(u, dict):
                raise ValueError("validation_failed")
            needed = ("id", "email", "display_name", "handle", "balance", "password_hash")
            if any(k not in u for k in needed):
                raise ValueError("validation_failed")
            if u["id"] != uid or not all(isinstance(u[k], str) for k in ("id", "email", "display_name", "handle", "password_hash")):
                raise ValueError("validation_failed")
            if not HANDLE_RE.match(u["handle"]) or not isinstance(u["balance"], int) or u["balance"] < 0:
                raise ValueError("validation_failed")
            if st["email_to_id"].get(u["email"]) != uid or st["handle_to_id"].get(u["handle"]) != uid:
                raise ValueError("validation_failed")
        if set(st["email_to_id"].values()) - set(users) or set(st["handle_to_id"].values()) - set(users):
            raise ValueError("validation_failed")
        if set(st["tokens"].values()) - set(users):
            raise ValueError("validation_failed")
        if any(not isinstance(uid, str) or uid not in users for uid in st["operators"]):
            raise ValueError("validation_failed")

        for pid, p in st["payments"].items():
            if not isinstance(pid, str) or not isinstance(p, dict) or p.get("id") != pid:
                raise ValueError("validation_failed")
            if p.get("from_user_id") not in users or p.get("to_user_id") not in users:
                raise ValueError("validation_failed")
            if not isinstance(p.get("amount"), int) or p["amount"] < 1 or p["amount"] > MAX_AMOUNT:
                raise ValueError("validation_failed")
            if not isinstance(p.get("note", ""), str) or p.get("visibility") not in ("public", "private") or not isinstance(p.get("created_at"), str):
                raise ValueError("validation_failed")
        for rid, r in st["requests"].items():
            if not isinstance(rid, str) or not isinstance(r, dict) or r.get("id") != rid:
                raise ValueError("validation_failed")
            if r.get("requester_id") not in users or r.get("payer_id") not in users:
                raise ValueError("validation_failed")
            if not isinstance(r.get("amount"), int) or r["amount"] < 0 or r["amount"] > MAX_AMOUNT:
                raise ValueError("validation_failed")
            if not isinstance(r.get("note", ""), str) or r.get("status") not in ("pending", "paid", "declined", "cancelled") or not isinstance(r.get("created_at"), str):
                raise ValueError("validation_failed")
            if r.get("payment_id") is not None and r["payment_id"] not in st["payments"]:
                raise ValueError("validation_failed")

        idem = {}
        for row in st["idem"]:
            if not isinstance(row, list) or len(row) != 5:
                raise ValueError("validation_failed")
            uid, key, method, path, rec = row
            if uid not in users or not all(isinstance(x, str) for x in (key, method, path)) or not isinstance(rec, dict):
                raise ValueError("validation_failed")
            if not isinstance(rec.get("sig"), str) or "response" not in rec:
                raise ValueError("validation_failed")
            idem[(uid, key, method, path)] = rec

        ns = copy.deepcopy(st)
        ns["operators"] = set(st["operators"])
        ns["idem"] = idem
        return ns


STORE = Store()


class Api(BaseHTTPRequestHandler):
    server_version = "PocketfulStage1/1.0"

    def log_message(self, *args):
        pass

    def send_json(self, status, body=None):
        self.send_response(status)
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def err(self, status, code):
        self.send_json(status, {"error": {"code": code, "message": code.replace("_", " ")}})

    def body(self):
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            if not raw:
                return {}
            val = json.loads(raw.decode("utf-8"))
            if not isinstance(val, dict):
                raise ValueError
            return val
        except Exception:
            raise RuntimeError("malformed_request")

    def auth(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer ") or len(h.split(" ", 1)[1]) == 0:
            raise PermissionError
        uid = STORE.state["tokens"].get(h.split(" ", 1)[1])
        if not uid:
            raise PermissionError
        return uid

    def user_public(self, uid):
        u = STORE.state["users"][uid]
        return u["id"], u["handle"], u["display_name"]

    def amount_ok(self, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
            return False
        return 1 <= int(v) <= MAX_AMOUNT

    def payment_doc(self, p):
        fu, th = STORE.state["users"][p["from_user_id"]], STORE.state["users"][p["to_user_id"]]
        return {"payment_id": p["id"], "from_user_id": fu["id"], "from_handle": fu["handle"],
                "to_user_id": th["id"], "to_handle": th["handle"], "amount": p["amount"],
                "currency": STORE.state["currency"], "note": p.get("note", ""),
                "visibility": p.get("visibility", "public"), "request_id": p.get("request_id"),
                "settlement_id": p.get("settlement_id"), "created_at": p["created_at"]}

    def request_doc(self, r):
        ru, pu = STORE.state["users"][r["requester_id"]], STORE.state["users"][r["payer_id"]]
        return {"request_id": r["id"], "requester_id": ru["id"], "requester_handle": ru["handle"],
                "payer_id": pu["id"], "payer_handle": pu["handle"], "amount": r["amount"],
                "currency": STORE.state["currency"], "note": r.get("note", ""), "status": r["status"],
                "payment_id": r.get("payment_id"), "created_at": r["created_at"]}

    def parse_page(self, q):
        def one(name, default):
            vals = q.get(name, [str(default)])
            s = vals[0]
            if not re.fullmatch(r"\d+", s):
                raise ValueError
            return int(s)
        limit, offset = one("limit", 50), one("offset", 0)
        if not (1 <= limit <= 200) or offset < 0:
            raise ValueError
        return limit, offset

    def idem(self, uid, body):
        key = self.headers.get("Idempotency-Key")
        if key is None or key == "":
            raise RuntimeError("missing_idempotency_key")
        if len(key) > 255:
            raise ValueError("validation_failed")
        sig = stable_json({"method": self.command, "path": urlparse(self.path).path, "body": body})
        scope = (uid, key, self.command, urlparse(self.path).path)
        rec = STORE.state["idem"].get(scope)
        if rec:
            if rec["sig"] != sig:
                raise FileExistsError
            return rec
        return {"scope": scope, "sig": sig}

    def save_idem(self, uid, rec, response):
        STORE.state["idem"][rec["scope"]] = {"sig": rec["sig"], "response": copy.deepcopy(response)}

    def do_GET(self):
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        with STORE.lock:
            try:
                if path == "/health":
                    return self.send_json(200, {"status": "ok"})
                if path == "/_test/export":
                    st = copy.deepcopy(STORE.state)
                    st["operators"] = list(st["operators"])
                    st["idem"] = [[uid, key, method, path, rec] for (uid, key, method, path), rec in st["idem"].items()]
                    return self.send_json(200, {"track": "pocketful", "format_version": 1, "state": st})
                uid = self.auth()
                if path == "/me":
                    u = STORE.state["users"][uid]
                    return self.send_json(200, {"user_id": uid, "display_name": u["display_name"], "handle": u["handle"],
                                                "balance": u["balance"], "currency": STORE.state["currency"],
                                                "minor_units": STORE.state["minor_units"]})
                if path == "/activity":
                    limit, offset = self.parse_page(q)
                    ps = [self.payment_doc(p) for p in STORE.state["payments"].values()
                          if p["visibility"] == "public" or uid in (p["from_user_id"], p["to_user_id"])]
                    ps.sort(key=lambda x: x["created_at"], reverse=True)
                    return self.send_json(200, {"payments": ps[offset:offset+limit], "has_more": len(ps) > offset + limit})
                if path == "/requests":
                    limit, offset = self.parse_page(q)
                    direction = q.get("direction", [None])[0]
                    status = q.get("status", [None])[0]
                    if direction not in (None, "incoming", "outgoing") or status not in (None, "pending", "paid", "declined", "cancelled"):
                        return self.err(422, "validation_failed")
                    rs = []
                    for r in STORE.state["requests"].values():
                        if uid not in (r["requester_id"], r["payer_id"]): continue
                        if direction == "incoming" and r["payer_id"] != uid: continue
                        if direction == "outgoing" and r["requester_id"] != uid: continue
                        if status and r["status"] != status: continue
                        rs.append(self.request_doc(r))
                    rs.sort(key=lambda x: x["created_at"], reverse=True)
                    return self.send_json(200, {"requests": rs[offset:offset+limit], "has_more": len(rs) > offset + limit})
                return self.err(404, "not_found")
            except PermissionError:
                return self.err(401, "unauthenticated")
            except ValueError:
                return self.err(422, "validation_failed")

    def do_POST(self):
        path = urlparse(self.path).path
        with STORE.lock:
            try:
                body = self.body()
                if path == "/_test/reset":
                    old = copy.deepcopy(STORE.state)
                    try:
                        STORE.reset(body)
                    except Exception:
                        STORE.state = old
                        return self.err(422, "validation_failed")
                    return self.send_json(204)
                if path == "/_test/import":
                    if body.get("track") != "pocketful" or body.get("format_version") != 1 or not isinstance(body.get("state"), dict):
                        return self.err(422, "validation_failed")
                    try:
                        st = STORE.validate_import_state(body["state"])
                    except ValueError:
                        return self.err(422, "validation_failed")
                    STORE.state = st
                    return self.send_json(204)
                if path == "/auth/signup":
                    return self.signup(body)
                if path == "/auth/login":
                    return self.login(body)
                uid = self.auth()
                if path == "/payments":
                    return self.create_payment(uid, body)
                if path == "/requests":
                    return self.create_request(uid, body)
                if path == "/splits":
                    return self.create_split(uid, body)
                if path == "/settlements":
                    return self.create_settlement(uid, body)
                m = re.fullmatch(r"/requests/([^/]+)/(pay|decline|cancel)", path)
                if m:
                    return self.request_action(uid, m.group(1), m.group(2), body)
                return self.err(404, "not_found")
            except RuntimeError as e:
                return self.err(400, str(e))
            except PermissionError:
                return self.err(401, "unauthenticated")
            except FileExistsError:
                return self.err(409, "idempotency_key_reuse")
            except ValueError as e:
                return self.err(422, str(e) if str(e) else "validation_failed")

    def signup(self, b):
        for k in ("email", "password", "display_name"):
            if k not in b:
                return self.err(422, "validation_failed")
            if not isinstance(b.get(k), str):
                return self.err(400, "malformed_request")
        if not all(isinstance(b.get(k), str) for k in ("email", "password", "display_name")):
            return self.err(400, "malformed_request")
        email = b["email"]
        if not EMAIL_RE.match(email) or len(b["password"]) < 8:
            return self.err(422, "validation_failed")
        if email in STORE.state["email_to_id"]:
            return self.err(409, "email_taken")
        handle = re.sub(r"[^a-z0-9_]", "_", email.split("@", 1)[0].lower())[:20]
        if handle in STORE.state["handle_to_id"]:
            return self.err(409, "handle_taken")
        uid, token = STORE.nid("u"), secrets.token_urlsafe(24)
        STORE.state["users"][uid] = {"id": uid, "email": email, "display_name": b["display_name"], "handle": handle, "balance": 0, "password_hash": hash_password(b["password"])}
        STORE.state["email_to_id"][email] = uid; STORE.state["handle_to_id"][handle] = uid; STORE.state["tokens"][token] = uid
        return self.send_json(201, {"user_id": uid, "display_name": b["display_name"], "token": token})

    def login(self, b):
        for k in ("email", "password"):
            if k not in b:
                return self.err(422, "validation_failed")
            if not isinstance(b.get(k), str):
                return self.err(400, "malformed_request")
        if not all(isinstance(b.get(k), str) for k in ("email", "password")):
            return self.err(400, "malformed_request")
        uid = STORE.state["email_to_id"].get(b["email"])
        if not uid or not verify_password(b["password"], STORE.state["users"][uid]["password_hash"]):
            return self.err(401, "unauthenticated")
        token = secrets.token_urlsafe(24); STORE.state["tokens"][token] = uid
        return self.send_json(200, {"user_id": uid, "display_name": STORE.state["users"][uid]["display_name"], "token": token})

    def check_note_visibility(self, b):
        note = b.get("note", "")
        vis = b.get("visibility", "public")
        if not isinstance(note, str) or len(note) > 200 or vis not in ("public", "private"):
            raise ValueError("validation_failed")
        return note, vis

    def create_payment_record(self, from_id, to_id, amount, note, vis, request_id=None, settlement_id=None, ts=None):
        p = {"id": STORE.nid("p"), "from_user_id": from_id, "to_user_id": to_id, "amount": amount,
             "note": note, "visibility": vis, "request_id": request_id, "settlement_id": settlement_id, "created_at": ts or now()}
        STORE.state["users"][from_id]["balance"] -= amount
        STORE.state["users"][to_id]["balance"] += amount
        STORE.state["payments"][p["id"]] = p
        return p

    def create_payment(self, uid, b):
        rec = self.idem(uid, b)
        if "response" in rec: return self.send_json(200, rec["response"])
        if not self.amount_ok(b.get("amount")) or not isinstance(b.get("to_handle"), str):
            return self.err(422, "validation_failed")
        note, vis = self.check_note_visibility(b)
        to_id = STORE.state["handle_to_id"].get(b["to_handle"])
        if not to_id: return self.err(404, "not_found")
        if to_id == uid: return self.err(422, "self_payment")
        amount = int(b["amount"])
        if STORE.state["users"][uid]["balance"] < amount: return self.err(409, "insufficient_funds")
        resp = self.payment_doc(self.create_payment_record(uid, to_id, amount, note, vis))
        self.save_idem(uid, rec, resp)
        return self.send_json(201, resp)

    def create_request(self, uid, b):
        rec = self.idem(uid, b)
        if "response" in rec: return self.send_json(200, rec["response"])
        if not self.amount_ok(b.get("amount")) or not isinstance(b.get("payer_handle"), str):
            return self.err(422, "validation_failed")
        note = b.get("note", "")
        if not isinstance(note, str) or len(note) > 200: return self.err(422, "validation_failed")
        payer = STORE.state["handle_to_id"].get(b["payer_handle"])
        if not payer: return self.err(404, "not_found")
        if payer == uid: return self.err(422, "self_request")
        r = {"id": STORE.nid("rq"), "requester_id": uid, "payer_id": payer, "amount": int(b["amount"]), "note": note, "status": "pending", "payment_id": None, "created_at": now()}
        STORE.state["requests"][r["id"]] = r
        resp = self.request_doc(r); self.save_idem(uid, rec, resp)
        return self.send_json(201, resp)

    def request_action(self, uid, rid, action, b):
        r = STORE.state["requests"].get(rid)
        if not r: return self.err(404, "not_found")
        if action == "pay":
            rec = self.idem(uid, b)
            if "response" in rec: return self.send_json(200, rec["response"])
            if uid != r["payer_id"]: return self.err(403, "forbidden")
            vis = b.get("visibility", "public")
            if vis not in ("public", "private"): return self.err(422, "validation_failed")
            if r["status"] != "pending": return self.err(409, "request_not_pending")
            if STORE.state["users"][uid]["balance"] < r["amount"]: return self.err(409, "insufficient_funds")
            p = self.create_payment_record(uid, r["requester_id"], r["amount"], r["note"], vis, rid)
            r["status"], r["payment_id"] = "paid", p["id"]
            resp = self.payment_doc(p); self.save_idem(uid, rec, resp)
            return self.send_json(201, resp)
        if action == "decline":
            if uid != r["payer_id"]: return self.err(403, "forbidden")
            if r["status"] == "declined": return self.send_json(200, self.request_doc(r))
            if r["status"] != "pending": return self.err(409, "request_not_pending")
            r["status"] = "declined"; return self.send_json(200, self.request_doc(r))
        if uid != r["requester_id"]: return self.err(403, "forbidden")
        if r["status"] == "cancelled": return self.send_json(200, self.request_doc(r))
        if r["status"] != "pending": return self.err(409, "request_not_pending")
        r["status"] = "cancelled"; return self.send_json(200, self.request_doc(r))

    def create_split(self, uid, b):
        rec = self.idem(uid, b)
        if "response" in rec: return self.send_json(200, rec["response"])
        hs = b.get("participant_handles")
        if not self.amount_ok(b.get("amount")) or not isinstance(hs, list) or not hs or any(not isinstance(h, str) for h in hs) or len(set(hs)) != len(hs):
            return self.err(422, "validation_failed")
        note = b.get("note", "")
        if not isinstance(note, str) or len(note) > 200: return self.err(422, "validation_failed")
        ids = []
        for h in hs:
            if h not in STORE.state["handle_to_id"]: return self.err(404, "not_found")
            ids.append(STORE.state["handle_to_id"][h])
        amount, base, extra = int(b["amount"]), int(b["amount"]) // len(hs), int(b["amount"]) % len(hs)
        split_id, ts, reqs, shares = STORE.nid("sp"), now(), [], []
        for i, (h, pid) in enumerate(zip(hs, ids)):
            share = base + (1 if i < extra else 0)
            shares.append({"handle": h, "amount": share})
            if pid != uid:
                r = {"id": STORE.nid("rq"), "requester_id": uid, "payer_id": pid, "amount": share, "note": note, "status": "pending", "payment_id": None, "created_at": ts}
                STORE.state["requests"][r["id"]] = r; reqs.append(self.request_doc(r))
        resp = {"split_id": split_id, "amount": amount, "currency": STORE.state["currency"], "note": note, "shares": shares, "requests": reqs, "created_at": ts}
        STORE.state["splits"][split_id] = resp; self.save_idem(uid, rec, resp)
        return self.send_json(201, resp)

    def create_settlement(self, uid, b):
        if uid not in STORE.state["operators"]: return self.err(403, "forbidden")
        rec = self.idem(uid, b)
        if "response" in rec: return self.send_json(200, rec["response"])
        trs = b.get("transfers")
        if not isinstance(trs, list) or not (1 <= len(trs) <= 32): return self.err(422, "validation_failed")
        parsed, net = [], {}
        for t in trs:
            if not isinstance(t, dict) or not self.amount_ok(t.get("amount")) or not isinstance(t.get("from_handle"), str) or not isinstance(t.get("to_handle"), str):
                return self.err(422, "validation_failed")
            note, vis = self.check_note_visibility(t)
            f = STORE.state["handle_to_id"].get(t["from_handle"]); to = STORE.state["handle_to_id"].get(t["to_handle"])
            if not f or not to: return self.err(404, "not_found")
            if f == to: return self.err(422, "self_payment")
            amount = int(t["amount"]); parsed.append((f, to, amount, note, vis))
            net[f] = net.get(f, 0) - amount; net[to] = net.get(to, 0) + amount
        if any(STORE.state["users"][u]["balance"] + d < 0 for u, d in net.items()):
            return self.err(409, "insufficient_funds")
        sid, ts = STORE.nid("st"), now()
        payments = [self.payment_doc(self.create_payment_record(f, to, amt, note, vis, None, sid, ts)) for f, to, amt, note, vis in parsed]
        resp = {"settlement_id": sid, "committed_at": ts, "payments": payments}
        STORE.state["settlements"][sid] = resp; self.save_idem(uid, rec, resp)
        return self.send_json(201, resp)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Api).serve_forever()
