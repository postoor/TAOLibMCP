"""
TAOLib — 桃園市立圖書館 (Taoyuan Public Library) HyLib app API client.

Reverse-engineered from the official Flutter app `taoyuan.hylib.library2` v1.1.25
and the webpac.typl.gov.tw Next.js web client.

Backend: https://webpac.typl.gov.tw/api/HyLibWS/graphql  (GraphQL, HyLibWS)

Auth / request model (REQUIRED for every GraphQL call):
  1. GET https://webpac.typl.gov.tw/  -> sets `HYSESSION` cookie + issues a
     per-session CSRF token (embedded in the page as `"csrfToken":"..."`).
  2. Every POST to the graphql endpoint MUST carry:
       - Cookie: HYSESSION (+ JSESSIONID once logged in), via a cookie jar
       - Header: X-CSRF-Token: <csrfToken>
     Without these the server returns HTTP 403 (Spring Security CSRF filter).
  3. Login: mutation appSSOLogin(user, pass) -> { appToken, refreshToken,
     readerCode, success, ... }. The password is sent in PLAINTEXT for the app
     login (the *web* login `ssoLogin` instead AES-encrypts user+pass and needs
     a captcha — the app path avoids both).
  4. Reader-scoped calls additionally send: Authorization: Bearer <appToken>.

All list-style resolvers return the generic `LayoutReturnResult` shape:
    { list { values [ { ref [ { key, value } ] } ] }  sort }
i.e. each row is a flat list of key/value pairs -> parsed to a dict by `_rows()`.
"""
import base64
import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.request

BASE = "https://webpac.typl.gov.tw"
GQL = BASE + "/api/HyLibWS/graphql"

# AES-256-CBC key used by the WEB client to encrypt credentials for `ssoLogin`.
# Recovered from the Next.js bundle (chunk 5989).
_WEB_AES_KEY_HEX = "a20b070dfdf921334a47ee5c076106068c718f11e97e4835e08acf19b6728684"

# Captcha XOR key: in the web client this is `atob(<bytes>)` where <bytes> is the
# ASCII of the recovered byte array. The getCaptcha `message` is a base64 blob
# that, once base64-decoded and XOR'd (cyclically) with this key, yields a
# payload whose last 18 bytes contain the human-readable answer; the real answer
# the server expects is answer[:3] + answer[-2:]  (recovered from chunk 5989).
_CAPTCHA_KEY_B64_BYTES = bytes([
    86, 83, 104, 56, 74, 51, 108, 83, 83, 121, 103, 51, 83, 84, 66, 111, 87, 72,
    104, 43, 82, 109, 107, 52, 83, 109, 86, 113, 78, 109, 90, 104, 76, 109, 70,
    101, 100, 88, 86, 49,
])


class TaoLibError(Exception):
    pass


class TaoLibClient:
    def __init__(self, username=None, password=None, timeout=30):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self.csrf = None
        self.app_token = None
        self.refresh_token = None
        self.reader_code = None
        self.web_session = False  # True once a reader-bound ssoLogin succeeds

    # ---- low level -------------------------------------------------------
    def bootstrap(self):
        """Establish a session cookie and grab the CSRF token (step 1)."""
        req = urllib.request.Request(BASE + "/", headers={"User-Agent": "Mozilla/5.0"})
        html = self._opener.open(req, timeout=self.timeout).read().decode("utf-8", "replace")
        m = re.search(r'"csrfToken":"([^"]+)"', html)
        if not m:
            raise TaoLibError("could not obtain csrfToken from homepage")
        self.csrf = m.group(1)
        return self.csrf

    def gql(self, query, variables=None, op=None, authed=False):
        if self.csrf is None:
            self.bootstrap()
        payload = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        if op:
            payload["operationName"] = op
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": BASE,
            "Referer": BASE + "/",
            "User-Agent": "Mozilla/5.0",
            "X-CSRF-Token": self.csrf,
        }
        if authed and not self.web_session:
            # app-token mode: reader identity carried by the Bearer token
            if not self.app_token:
                raise TaoLibError("not logged in: call login() first")
            headers["Authorization"] = "Bearer " + self.app_token
        # web_session mode: reader is bound to the HYSESSION cookie (set by
        # ssoLogin); cookie + X-CSRF-Token alone authenticate the request.
        req = urllib.request.Request(GQL, data=data, headers=headers, method="POST")
        try:
            resp = self._opener.open(req, timeout=self.timeout)
            body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TaoLibError("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
        if isinstance(body, dict) and body.get("errors"):
            msgs = "; ".join(e.get("message", "") for e in body["errors"])
            raise TaoLibError("GraphQL error: " + msgs[:300])
        return body.get("data") if isinstance(body, dict) else body

    @staticmethod
    def _rows(layout):
        """Flatten a LayoutReturnResult -> list[dict]."""
        if not layout:
            return []
        lst = layout.get("list") or {}
        out = []
        for row in lst.get("values", []) or []:
            ref = row.get("ref") if isinstance(row, dict) else None
            if isinstance(ref, list):
                out.append({x["key"]: x["value"] for x in ref})
            else:
                out.append(row)
        return out

    # ---- auth ------------------------------------------------------------
    _LOGIN = (
        "mutation appSSOLogin($account: String!, $password: String!) {"
        "  appSSOLogin(user: $account, pass: $password) {"
        "    appToken refreshToken message success licenseStatus errorType"
        "    chPaLink readerCode ttl } }"
    )
    _REFRESH = (
        "mutation refreshAppToken($refreshToken: String!) {"
        "  refreshAppToken(refreshToken: $refreshToken) {"
        "    appToken refreshToken } }"
    )

    def login(self, username=None, password=None):
        user = username or self.username
        pw = password or self.password
        if not user or not pw:
            raise TaoLibError("username/password required")
        self.bootstrap()  # fresh session + csrf
        d = self.gql(self._LOGIN, {"account": user, "password": pw}, op="appSSOLogin")
        r = d["appSSOLogin"]
        if not r.get("success"):
            raise TaoLibError("login failed: " + (r.get("message") or "unknown"))
        self.app_token = r["appToken"]
        self.refresh_token = r.get("refreshToken")
        self.reader_code = r.get("readerCode")
        return r

    def refresh(self):
        if not self.refresh_token:
            raise TaoLibError("no refresh token")
        d = self.gql(self._REFRESH, {"refreshToken": self.refresh_token}, op="refreshAppToken")
        r = d["refreshAppToken"]
        self.app_token = r["appToken"]
        self.refresh_token = r.get("refreshToken") or self.refresh_token
        return r

    # ---- web login (ssoLogin) — binds reader to the session -------------
    # Needed for reader-scoped data (loans / reservations / history / ILL),
    # which the lightweight appSSOLogin token does NOT bind. Requires the AES
    # credential encryption + the self-contained captcha solve below.
    @staticmethod
    def _aes_encrypt(plain):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as _pad
        key = bytes.fromhex(_WEB_AES_KEY_HEX)
        iv = os.urandom(16)
        padder = _pad.PKCS7(128).padder()
        data = padder.update(plain.encode()) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return iv.hex() + ":" + base64.b64encode(enc.update(data) + enc.finalize()).decode()

    # tesseract language to use; this box ships only `afr`, which reads the
    # plain Latin digits fine. Override via TAOLIB_TESS_LANG (e.g. "eng").
    _TESS_LANG = os.environ.get("TAOLIB_TESS_LANG", "afr")

    def _fetch_captcha_png(self):
        """getCaptcha -> decrypt -> raw PNG bytes of the 5-digit image.

        The `message` is base64; base64-decode then cyclic-XOR with the key
        yields a PNG followed by an 18-byte `hyw<ms-timestamp>eb` session tag
        (NOT the answer — the answer is drawn in the image)."""
        d = self.gql("query getCaptcha{ getCaptcha{ success message } }", op="getCaptcha")
        msg = base64.b64decode(d["getCaptcha"]["message"])
        key = base64.b64decode(_CAPTCHA_KEY_B64_BYTES.decode())
        out = bytearray(len(msg))
        s = 0
        for i, b in enumerate(msg):
            out[i] = b ^ key[s]
            s = s + 1 if s < len(key) - 1 else 0
        out = bytes(out)
        end = out.rfind(b"IEND")
        return out[: end + 8] if end != -1 else out

    def _solve_captcha(self):
        """Return the 5-digit captcha answer by OCR'ing the decrypted image."""
        import io
        import subprocess
        from PIL import Image
        png = self._fetch_captcha_png()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        w, h = img.size
        px = img.load()
        # min-channel binarisation: white bg stays bright, coloured digits go dark
        bw = Image.new("L", (w, h), 255)
        bp = bw.load()
        for y in range(h):
            for x in range(w):
                r, g, b = px[x, y]
                bp[x, y] = 0 if min(r, g, b) < 180 else 255
        bw = bw.resize((w * 5, h * 5))
        buf = io.BytesIO()
        bw.save(buf, format="PNG")
        proc = subprocess.run(
            ["tesseract", "-", "-", "-l", self._TESS_LANG, "--psm", "7",
             "-c", "tessedit_char_whitelist=0123456789"],
            input=buf.getvalue(), capture_output=True, timeout=30,
        )
        return "".join(ch for ch in proc.stdout.decode("utf-8", "replace") if ch.isdigit())

    def login_web(self, username=None, password=None, retries=4):
        """Reader-bound login via ssoLogin (AES-encrypted creds + captcha).

        On success the HYSESSION cookie is reader-bound and all reader-scoped
        queries work with cookie + X-CSRF-Token (no Bearer)."""
        user = username or self.username
        pw = password or self.password
        if not user or not pw:
            raise TaoLibError("username/password required")
        q = ("mutation SSOLogin($user:String!,$pass:String!,$captcha:String){"
             " ssoLogin(user:$user,pass:$pass,captcha:$captcha,encrypt:true){"
             " success message sessionID errorType licenseStatus ttl } }")
        last = None
        for _ in range(retries):
            self.bootstrap()
            cap = self._solve_captcha()
            d = self.gql(q, {"user": self._aes_encrypt(user),
                             "pass": self._aes_encrypt(pw),
                             "captcha": cap}, op="SSOLogin")
            r = d["ssoLogin"]
            last = r.get("message")
            if r.get("success"):
                self.web_session = True
                self.reader_code = user
                return r
            # captcha.* / textinbox => wrong captcha; retry with a fresh one
        raise TaoLibError("ssoLogin failed: " + str(last))

    def _ensure(self):
        if self.web_session:
            return
        if not self.app_token:
            self.login()

    # ---- catalogue / search (找書) --------------------------------------
    def search(self, keyword, field="FullText", page=1, limit=20, op="AND"):
        """Full catalogue search. Returns list[dict] of book records.

        `field` is a HyLib search index code (e.g. FullText, Title, Author, ISBN).
        Record id for detail lookups is the `sid` key of each row.
        SearchForm uses parallel arrays: searchField[]/searchInput[]/op[].
        """
        self._ensure()
        q = (
            "query Search($f:[String],$in:[String],$op:[String],$p:Int,$l:Int){"
            "  search(Input:{searchField:$f,searchInput:$in,op:$op,pageNo:$p,limit:$l}){"
            "    list{values{ref{key value}}} } }"
        )
        d = self.gql(q, {"f": [field], "in": [keyword], "op": [op], "p": page, "l": limit},
                     op="Search", authed=True)
        return self._rows(d["search"])

    def book_detail(self, sid):
        """Full bibliographic detail for a record id (the `sid` from search)."""
        self._ensure()
        q = ("query bookDetail($id:Int!){ getBookDetail(id:$id){"
             " view{ list{ values{ ref{ key value } } } } } }")
        d = self.gql(q, {"id": int(sid)}, op="bookDetail", authed=True)
        return self._rows((d.get("getBookDetail") or {}).get("view"))

    def holdings(self, sid, can_show=1):
        """Copy/availability info across all library sites for a record."""
        self._ensure()
        q = ("query getHoldAllKeepSite($marcId:Int!,$c:Int!){"
             " getHoldAllKeepSite(Input:{marcId:$marcId,canShowHoldLendAndRead:$c}){"
             " list{values{ref{key value}}} } }")
        d = self.gql(q, {"marcId": int(sid), "c": can_show}, op="getHoldAllKeepSite", authed=True)
        return self._rows(d.get("getHoldAllKeepSite"))

    # ---- reader data -----------------------------------------------------
    def reader_info(self):
        self._ensure()
        q = ("query getReaderInfo{ getReaderInfo{ reader_id reader_code reader_name"
             " nickname expired_date lendfilecnt reservecnt"
             " license_status{id name code} reader_type{id code name} } }")
        d = self.gql(q, op="getReaderInfo", authed=True)
        return d.get("getReaderInfo")

    def current_loans(self):
        """借閱中 — books currently checked out by the reader."""
        self._ensure()
        q = ("query getLendFile{ getLendFile(Input:{}){ list{values{ref{key value}}} } }")
        d = self.gql(q, op="getLendFile", authed=True)
        return self._rows(d.get("getLendFile"))

    def borrow_history(self):
        """借閱歷史 — past loans."""
        self._ensure()
        q = ("query getHistory{ getHistory(Input:{}){ list{values{ref{key value}}} } }")
        d = self.gql(q, op="getHistory", authed=True)
        return self._rows(d.get("getHistory"))

    def reservations(self, arrived=False):
        """預約 — holds. arrived=False: queued/not yet available;
        arrived=True: ready for pickup."""
        self._ensure()
        flag = 'showreadbookArrived:"1"' if arrived else 'showreadbook:"1"'
        q = ("query getReadBook{ getReadBook(Input:{%s}){"
             " list{values{ref{key value}}} } }" % flag)
        d = self.gql(q, op="getReadBook", authed=True)
        return self._rows(d.get("getReadBook"))

    def cross_borrow_list(self):
        """通閱 — inter-library-loan (跨館通閱) items in progress."""
        self._ensure()
        q = ("query getCrossBorrowList{ getTrAppMainProcess(Input:{showreadbook:\"1\"}){"
             " list{values{ref{key value}}} } }")
        d = self.gql(q, op="getCrossBorrowList", authed=True)
        return self._rows(d.get("getTrAppMainProcess"))

    def sites(self):
        """All library branches / pickup locations (keepSiteId + code)."""
        self._ensure()
        q = ("query getSiteList{ getAllKeepSiteDataList{"
             " keepsiteName keepSiteId keepsiteCode } }")
        d = self.gql(q, op="getSiteList", authed=True)
        return d.get("getAllKeepSiteDataList") or []

    # ---- state-changing actions (write) ---------------------------------
    # NOTE: these mutate the real library account (place/cancel holds, renew,
    # request ILL). Query shapes recovered from the app; argument semantics
    # documented in README. Guard with explicit user intent before calling.
    def place_hold(self, hold_id, pickup_keep_site_id, reader_valid_date):
        """預約下單 — place a hold. reader_valid_date = reader card expiry (YYYY-MM-DD)."""
        self._ensure()
        q = ("mutation getHoldReserve($holdId:Int!,$pickupKeepSiteId:Int!,$readerValiddate:String!){"
             " getHoldReserve(holdId:$holdId,pickupKeepSiteId:$pickupKeepSiteId,readerValiddate:$readerValiddate){"
             " success message total } }")
        d = self.gql(q, {"holdId": int(hold_id), "pickupKeepSiteId": int(pickup_keep_site_id),
                         "readerValiddate": reader_valid_date}, op="getHoldReserve", authed=True)
        return d.get("getHoldReserve")

    def cancel_reservation(self, reserve_id):
        """取消預約 — cancel a hold by its reservation id."""
        self._ensure()
        q = ("mutation cancelBooking($id:Int!){ doCancelReserve(id:$id){ success message } }")
        d = self.gql(q, {"id": int(reserve_id)}, op="cancelBooking", authed=True)
        return d.get("doCancelReserve")

    def renew_books(self, ids):
        """續借 — renew one or more current loans by their lendfile ids."""
        self._ensure()
        q = ("mutation batchContuineBook($ids:[Int]!){"
             " batchContuineBook(ids:$ids){ message success errorType } }")
        d = self.gql(q, {"ids": [int(i) for i in ids]}, op="batchContuineBook", authed=True)
        return d.get("batchContuineBook")


def _read_env(env_path=None):
    env = {}
    path = env_path or os.environ.get("TAOLIB_ENV") or os.path.join(os.getcwd(), ".env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k] = v
    return env


def client_from_env(env_path=None):
    """Build a single client from a .env file with USERNAME / PASSWORD."""
    env = _read_env(env_path)
    user = os.environ.get("TAOLIB_USERNAME") or env.get("USERNAME")
    pw = os.environ.get("TAOLIB_PASSWORD") or env.get("PASSWORD")
    return TaoLibClient(username=user, password=pw)


def load_accounts(path=None):
    """Return a list of accounts: [{name, username, password}, ...].

    Resolution order:
      1. `accounts.json` (path arg, or $TAOLIB_ACCOUNTS, or ./accounts.json):
         either a list of {name?, username, password} or {"accounts": [...]}.
         `name` defaults to `username`; the first entry is the default account.
      2. Fallback: the single USERNAME/PASSWORD from .env (name = username).
    """
    p = path or os.environ.get("TAOLIB_ACCOUNTS") or os.path.join(os.getcwd(), "accounts.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("accounts") if isinstance(data, dict) else data
        out = []
        for a in raw or []:
            if not a.get("username") or not a.get("password"):
                continue
            out.append({"name": str(a.get("name") or a["username"]),
                        "username": a["username"], "password": a["password"]})
        if out:
            return out
    c = client_from_env()
    if c.username and c.password:
        return [{"name": c.username, "username": c.username, "password": c.password}]
    return []


class ClientManager:
    """Lazily builds and caches one authenticated client per named account."""

    def __init__(self, accounts=None, prefer_web=True):
        self.accounts = accounts if accounts is not None else load_accounts()
        self._clients = {}
        self.prefer_web = prefer_web

    def names(self):
        return [a["name"] for a in self.accounts]

    def default_name(self):
        if not self.accounts:
            raise TaoLibError("no accounts configured (set accounts.json or .env)")
        return self.accounts[0]["name"]

    def resolve(self, account=None):
        """Map an account selector (name or username, or None=default) to a name."""
        if account is None or account == "":
            return self.default_name()
        for a in self.accounts:
            if a["name"] == account or a["username"] == account:
                return a["name"]
        raise TaoLibError("unknown account: %s (have: %s)" % (account, ", ".join(self.names())))

    def get(self, account=None):
        name = self.resolve(account)
        c = self._clients.get(name)
        if c is None:
            a = next(x for x in self.accounts if x["name"] == name)
            c = TaoLibClient(username=a["username"], password=a["password"])
            self._clients[name] = c
        if not c.web_session and not c.app_token:
            if self.prefer_web:
                try:
                    c.login_web()
                except Exception:
                    c.login()
            else:
                c.login()
        return c
