# TAOLib — 桃園市立圖書館 API (reverse-engineered) + MCP server

反解官方 App **桃園市立圖書館** (`taoyuan.hylib.library2`, v1.1.25, Flutter) 後，
將其登入、找書、借閱中、預約、通閱等功能整理成一個 **MCP server**，讓 agent 直接呼叫。

後端為凌網科技 HyLib 系統：GraphQL endpoint
`https://webpac.typl.gov.tw/api/HyLibWS/graphql`。

---

## 1. 逆向過程摘要

| 項目 | 發現 |
|------|------|
| App 型態 | Flutter（業務邏輯在 Dart AOT `libapp.so`，Java 層僅殼） |
| API | 單一 GraphQL endpoint `/api/HyLibWS/graphql`（HyLibWS） |
| 主機架構 | `/` 是 Node.js(Next.js) 前端；`/api/HyLibWS/*` 是 Java/Tomcat 後端 |
| GraphQL introspection | **已停用**（改用「錯誤訊息型別洩漏」反推 schema） |
| 操作數 | 從 `libapp.so` 抽出 81 個 query/mutation |

App 的 GraphQL query 字串直接嵌在 `libapp.so`；Web 版 (Next.js) 的 chunk 則提供
乾淨的 query/變數結構與加密金鑰，兩者交叉比對得到完整 API。

### 認證／請求模型（每個請求都需要）

1. `GET https://webpac.typl.gov.tw/` → 取得 `HYSESSION` cookie，並從頁面取出
   per-session 的 `csrfToken`（`"csrfToken":"..."`）。
2. 每個 GraphQL POST 都必須帶：
   - **Cookie**：`HYSESSION`（登入後再加 `JSESSIONID`）→ 用 cookie jar 維持
   - **Header**：`X-CSRF-Token: <csrfToken>`
   - 缺任一個 → 伺服器回 **HTTP 403**（Spring Security CSRF filter）
3. **兩種登入**（重點）：
   - `appSSOLogin(user, pass)`（明文、免驗證碼）→ `{ appToken, readerCode }`。
     **但 appToken 只認證請求，不會把 reader 綁到 session** → 借閱中/預約/通閱/
     歷史、`getReaderInfo` 全部回空/`null`。適合只做找書/書目/館藏的情境。
   - `ssoLogin(user, pass, captcha, encrypt:true)` → 真正把 reader 綁到 `HYSESSION`
     → 所有讀者資料可讀（cookie + `X-CSRF-Token`，不需 Bearer）。帳密需
     **AES-256-CBC** 加密，且要過**驗證碼**。
4. **驗證碼自解**（`login_web` 內建）：`getCaptcha.message` 是 base64；decode 後對
   固定金鑰做 cyclic-XOR 得到一張 PNG（末 18 bytes 是 `hyw<timestamp>eb` 的 session
   tag，非答案）。答案是畫在圖上的 5 位數字，用本機 `tesseract` 辨識（自動挑已安裝
   的拉丁模型，優先 `eng` 再 `afr`；可用 `TAOLIB_TESS_LANG` 指定）。

> `TaoLibClient` 兩種登入：`login()`（app token，輕量，僅非讀者資料）與
> `login_web()`（reader-bound，讀者資料必用）。MCP server 預設走 `login_web()`。

### 關鍵 GraphQL 操作對照

| 功能 | 操作 | 簽章（已驗證） |
|------|------|------|
| 登入 | `appSSOLogin` | `appSSOLogin(user,pass){appToken refreshToken readerCode success}` |
| Token 更新 | `refreshAppToken` | `refreshAppToken(refreshToken){appToken refreshToken}` |
| 找書 | `search` | `search(Input:{searchField:[String],searchInput:[String],op:[String],pageNo,limit})` |
| 書目詳情 | `getBookDetail` | `getBookDetail(id:Int)` （id = search 結果的 `sid`） |
| 館藏狀態 | `getHoldAllKeepSite` | `getHoldAllKeepSite(Input:{marcId,canShowHoldLendAndRead})` |
| 借閱中 | `getLendFile` | `getLendFile(Input:{})` |
| 借閱歷史 | `getHistory` | `getHistory(Input:{})` |
| 預約(排隊/已到) | `getReadBook` | `getReadBook(Input:{showreadbook:"1"})` / `{showreadbookArrived:"1"}` |
| 預約(依證號) | `getReadBookByReaderCode` | `getReadBookByReaderCode(Input:{readerCodeList:[String]})` |
| 通閱(跨館) | `getTrAppMainProcess` | `getTrAppMainProcess(Input:{showreadbook:"1"})` |
| 分館清單 | `getAllKeepSiteDataList` | `{keepsiteName keepSiteId keepsiteCode}` |
| 預約下單 | `getHoldReserve` | `getHoldReserve(holdId,pickupKeepSiteId,readerValiddate)` |
| 取消預約 | `doCancelReserve` | `doCancelReserve(id:Int)` |
| 續借 | `batchContuineBook` | `batchContuineBook(ids:[Int])` |
| 可通閱館藏 | `getApplyList` | `getApplyList(Input:{marcID,pageNo})` → `items{id,...}` |
| 申請通閱 | `apply` | `apply(Input:{holdId,pickupKeepSite,mail2,tel,tel2})` |
| 取消通閱 | `doCancelApply` | `doCancelApply(id:Int)` |

所有清單型 resolver 回傳通用 `LayoutReturnResult`：
`{ list { values [ { ref [ { key, value } ] } ] } }`，每列是一組 key/value，
`client._rows()` 會攤平成 `dict`。找書每列含 `sid`(記錄id)、`title`、`author`、
`isbn`、`publisher`、`pubyear`、`isCanLend` 等。

---

## 2. 安裝 / 設定

- 需求：Python 3.8+。
  - 僅 `login()`（找書/書目/館藏）：stdlib 即可。
  - `login_web()`（讀者資料，MCP 預設）：另需 `cryptography`、`Pillow`，以及系統
    `tesseract`（任一 Latin 數字模型，如 `afr`/`eng`）。
- 憑證（擇一）：
  - **單帳號**：專案根目錄 `.env`
    ```
    USERNAME=你的借閱證號
    PASSWORD=你的密碼
    ```
    或環境變數 `TAOLIB_USERNAME` / `TAOLIB_PASSWORD` / `TAOLIB_ENV`。
  - **多帳號**：`accounts.json`（見 `accounts.example.json`），第一個為預設；
    `name` 省略時以 `username` 當名稱：
    ```json
    { "accounts": [
        { "name": "我",   "username": "TY0000001", "password": "pw1" },
        { "name": "家人", "username": "TY0000002", "password": "pw2" }
    ] }
    ```
    路徑預設 `./accounts.json`，可用 `TAOLIB_ACCOUNTS` 覆寫。每個帳號各自維持
    獨立 session（獨立 cookie jar + 各自登入）。

### 在 Claude Code 使用（MCP）

專案已附 `.mcp.json`，在此目錄啟動 Claude Code 即會載入 `taolib` MCP server，
提供工具：`list_accounts`、`search_books`、`book_detail`、`book_holdings`、
`my_current_loans`、`my_borrow_history`、`my_reservations`、`my_cross_borrow`、
`list_sites`、`reader_info`、`place_hold`、`cancel_reservation`、`renew_books`。

**多帳號**：除 `list_accounts` 外，每個工具都接受可選的 `account` 參數（帳號
名稱或借閱證號）；省略即用預設（第一個）帳號。先用 `list_accounts` 查有哪些。

手動加入其他 MCP client 的設定：
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

### 當成 Python library
```python
# 單帳號
from taolib.client import client_from_env
c = client_from_env()
c.login_web()                 # 讀者資料需用 login_web（含驗證碼自解）
for b in c.search("貓", limit=5):
    print(b["sid"], b["title"], b["author"])
print(c.current_loans())            # 借閱中
print(c.reservations(arrived=True)) # 已到館可取
print(c.cross_borrow_list())        # 通閱

# 多帳號
from taolib.client import ClientManager
m = ClientManager()                 # 讀 accounts.json（或 .env fallback）
print(m.names())                    # ['我', '家人']
me   = m.get("我")                  # 各帳號獨立 session，首次呼叫才登入
fam  = m.get("家人")
print(len(me.current_loans()), len(fam.current_loans()))
```

---

## 3. 寫入動作（會更動真實帳號）

`place_hold` / `cancel_reservation` / `renew_books` 會真的預約/取消/續借。
query 結構已從 App 還原，但**未在測試帳號上實際觸發**（避免外部副作用）；
使用前請確認讀者意圖。

---

## 4. 已驗證範圍

以 `.env` 測試帳號 `login_web()` 實測（HTTP 200、真實資料）：
- 登入（ssoLogin + 自解驗證碼）、`reader_info`（讀者姓名）
- 找書（真實書目）、書目詳情（37 欄）、館藏狀態、分館清單（43）
- **借閱中 30 筆**（含 title/returndate/lenddate/barcode/len_id/續借資訊）、
  **借閱歷史 30 筆**、預約（排隊/已到，當前 0）、通閱（當前 0）
- MCP server：initialize / tools/list / tools/call（`my_current_loans` 回 30）

未實際觸發的只有寫入動作（見下節）。
