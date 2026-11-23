# INSTALL — TAOLib MCP (for agents)

Setup guide for an agent wiring up the `taolib` MCP server (桃園市立圖書館 API).
Follow top to bottom; every step has a command and a check. Replace
`/home/lksdub/Workspace/TAOLib` with the actual repo path if different.

What this gives you: an MCP server whose tools let you search the catalogue and
read/act on a reader's account (loans, reservations, cross-borrow, renew) for
one or many library accounts. See `README.md` for API/reverse-engineering detail.

---

## 0. Prerequisites

- Python 3.8+  →  `python3 --version`
- (reader-scoped data only) a system `tesseract` + one Latin digit model.
  - Check: `tesseract --list-langs`  — any Latin model (`eng`, `afr`, ...) works;
    the client auto-detects it (prefers `eng`, then `afr`). The login captcha is
    solved locally by OCR'ing 5 plain digits; no external OCR service is used.

If you ONLY need catalogue search / book detail / holdings, you can skip
tesseract and the Python deps entirely (those use the stdlib-only `login()`).

---

## 1. Install Python dependencies

Reader-scoped data (loans/reservations/history/cross-borrow/renew) needs:

```bash
cd /home/lksdub/Workspace/TAOLib
python3 -m pip install -r requirements.txt
```

Check:
```bash
python3 -c "import cryptography, PIL; print('deps ok')"
```

If `tesseract --list-langs` shows no Latin model, install one via the OS package
manager (NOT pip):
- Debian/Ubuntu: `sudo apt install tesseract-ocr tesseract-ocr-eng`
- Arch:          `sudo pacman -S tesseract tesseract-data-eng`

The client auto-detects the installed model, so no extra config is needed. To
force a specific one, set `TAOLIB_TESS_LANG` (e.g. `eng`).

---

## 2. Configure credentials

Pick ONE of the two. Both keep secrets out of git (`.env` and `accounts.json`
are in `.gitignore` — never commit them).

### Single account — `.env`
```
USERNAME=借閱證號
PASSWORD=密碼
```
(Or export `TAOLIB_USERNAME` / `TAOLIB_PASSWORD`, or point `TAOLIB_ENV` at a file.)

### Multiple accounts — `accounts.json`
Copy the template and fill it in (first entry = default account):
```bash
cp accounts.example.json accounts.json
# then edit accounts.json
```
```json
{ "accounts": [
    { "name": "我",   "username": "TY0000001", "password": "pw1" },
    { "name": "家人", "username": "TY0000002", "password": "pw2" }
] }
```
Override its path with `TAOLIB_ACCOUNTS=/abs/path/accounts.json` if needed.
If `accounts.json` is absent, the server falls back to `.env`.

---

## 3. Verify the client (end-to-end, hits the live server)

```bash
cd /home/lksdub/Workspace/TAOLib
PYTHONPATH=. python3 -c "
from taolib.client import ClientManager
m = ClientManager()
print('accounts:', m.names())
c = m.get()                       # first/default account; logs in on first use
print('reader:', (c.reader_info() or {}).get('reader_name'))
print('current loans:', len(c.current_loans()))
print('search hit:', c.search('貓', limit=1)[0]['title'])
"
```
Expect: account list, the reader's name, a loan count, and a search title.
`login failed` / `ssoLogin failed: ...captcha...` → see Troubleshooting.

---

## 4. Register the MCP server

A ready `.mcp.json` ships in the repo (loaded automatically when Claude Code
starts in this directory). To register elsewhere, add:

```json
{
  "mcpServers": {
    "taolib": {
      "command": "python3",
      "args": ["-m", "taolib.mcp_server"],
      "cwd": "/home/lksdub/Workspace/TAOLib",
      "env": {
        "PYTHONPATH": "/home/lksdub/Workspace/TAOLib",
        "TAOLIB_ENV": "/home/lksdub/Workspace/TAOLib/.env"
      }
    }
  }
}
```
For multi-account add `"TAOLIB_ACCOUNTS": "/home/lksdub/Workspace/TAOLib/accounts.json"`.

Verify the server speaks MCP over stdio (no client needed):
```bash
cd /home/lksdub/Workspace/TAOLib
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"my_current_loans","arguments":{}}}' \
| PYTHONPATH=. python3 -m taolib.mcp_server
```
Expect three JSON lines: serverInfo, the tool list, and a loans result.

---

## 5. Tools (what you can call)

| Tool | 用途 | 參數 |
|------|------|------|
| `list_accounts` | 列出已設定帳號 | — |
| `search_books` | 找書 | `keyword`, `field?`, `page?`, `limit?` |
| `book_detail` | 書目詳情 | `sid` (search 結果的 sid) |
| `book_holdings` | 各館館藏狀態 | `sid` |
| `my_current_loans` | 借閱中 | — |
| `my_borrow_history` | 借閱歷史 | — |
| `my_reservations` | 預約 | `arrived?` (true=已到) |
| `my_cross_borrow` | 通閱 | — |
| `list_sites` | 分館/取書點 | — |
| `reader_info` | 讀者資訊 | — |
| `place_hold` | 預約下單 (寫入) | `holdId`, `pickupKeepSiteId`, `readerValidDate` |
| `cancel_reservation` | 取消預約 (寫入) | `reserveId` |
| `renew_books` | 續借 (寫入) | `ids` (借閱中的 `len_id`) |
| `cross_borrow_options` | 可通閱館藏 | `sid`, `page?` (items.id = holdId) |
| `cross_borrow_apply` | 申請通閱 (寫入) | `holdId`, `pickupKeepSite`, `mail2?`, `tel?`, `tel2?` |
| `cancel_cross_borrow` | 取消通閱 (寫入) | `applyId` (my_cross_borrow 的項目 id) |

Every tool except `list_accounts` also takes optional `account` (name or card
number); omit for the default account. The three write tools change the real
account — confirm intent before calling.

---

## 6. Troubleshooting

- **`no accounts configured`** — no `.env` and no `accounts.json`. Do step 2.
- **HTTP 403** — the server-side session/CSRF expires after a while; the client
  now auto-re-logins and retries once on a 403, so this is handled transparently.
  If 403 persists on *every* call from the start, it is usually a network block
  to `webpac.typl.gov.tw` — confirm `curl -s -o /dev/null -w '%{http_code}' https://webpac.typl.gov.tw/` returns 200.
- **`tesseract not found ...`** — install it + a Latin model (step 0/1), e.g.
  `apt install tesseract-ocr tesseract-ocr-eng`.
- **`ssoLogin failed: ...captcha...`** — OCR missed the digits. `login_web`
  already retries; if it persists, install the `eng` model and/or check
  `tesseract --list-langs`. Force a model with `TAOLIB_TESS_LANG=eng`.
- **Loans empty but the account has loans** — the server fell back to the
  token-only `login()` (no reader binding). Ensure deps + tesseract are present
  so `login_web()` succeeds (step 1).
- **Wrong account acted on** — pass `account` explicitly; `list_accounts` shows
  names and which is default (the first one).
