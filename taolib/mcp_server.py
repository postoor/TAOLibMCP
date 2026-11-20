#!/usr/bin/env python3
"""
TAOLib MCP server — exposes the 桃園市立圖書館 (Taoyuan Public Library) HyLib
API as MCP tools an agent can call.

Transport: stdio, JSON-RPC 2.0 (MCP 2024-11-05). No third-party MCP SDK needed —
only Python stdlib + `cryptography` (the latter only if the web-login path is
ever used; the default app-login path needs stdlib alone).

Credentials are read once from env (TAOLIB_USERNAME/TAOLIB_PASSWORD) or a .env
file (TAOLIB_ENV, else ./.env with USERNAME=/PASSWORD=). The client keeps one
authenticated session alive for the lifetime of the server process.

Run standalone (for registration in an MCP client):
    python3 -m taolib.mcp_server
Example Claude Code settings.json entry:
    "mcpServers": {
      "taolib": {
        "command": "python3",
        "args": ["-m", "taolib.mcp_server"],
        "cwd": "/home/lksdub/Workspace/TAOLib",
        "env": {"TAOLIB_ENV": "/home/lksdub/Workspace/TAOLib/.env"}
      }
    }
"""
import json
import sys
import traceback

from taolib.client import ClientManager, TaoLibError

PROTOCOL_VERSION = "2024-11-05"
_manager = None


def manager():
    global _manager
    if _manager is None:
        # prefer_web -> reader-bound ssoLogin (+ self-contained captcha solve),
        # required for loans/reservations/history/ILL; falls back to token login.
        _manager = ClientManager(prefer_web=True)
    return _manager


def client(account=None):
    """Authenticated client for the selected account (None = default)."""
    return manager().get(account)


# -- tool definitions: (name, description, input schema, handler) -----------
# Every tool accepts an optional `account` (name or card number) to pick which
# configured account to act as; omit it to use the default (first) account.
_ACCOUNT_PROP = {"account": {"type": "string",
                             "description": "帳號名稱或借閱證號 (多帳號時選擇；省略=預設帳號)"}}


def _tools():
    def obj(props=None, required=None):
        d = {"type": "object", "properties": dict(_ACCOUNT_PROP, **(props or {}))}
        if required:
            d["required"] = required
        return d

    def acct(a):
        return a.get("account")

    return [
        ("list_accounts",
         "列出已設定的帳號 (供其他工具的 account 參數使用)；第一個為預設。",
         {"type": "object", "properties": {}},
         lambda a: {"accounts": manager().names(), "default": manager().default_name()}),

        ("search_books",
         "找書：搜尋館藏目錄。回傳書目清單，每本含 sid(記錄id,供 book_detail/holdings 使用)、title、author、isbn、publisher、pubyear、isCanLend 等。",
         obj({
             "keyword": {"type": "string", "description": "搜尋關鍵字"},
             "field": {"type": "string", "description": "索引欄位代碼 (FullText/Title/Author/ISBN...)，預設 FullText"},
             "page": {"type": "integer", "description": "頁碼，預設 1"},
             "limit": {"type": "integer", "description": "每頁筆數，預設 20"},
         }, ["keyword"]),
         lambda a: client(acct(a)).search(a["keyword"], a.get("field", "FullText"),
                                          a.get("page", 1), a.get("limit", 20))),

        ("book_detail",
         "書目詳情：以 sid(來自 search_books) 取得完整書目資料。",
         obj({"sid": {"type": "integer", "description": "書目記錄 id (search 結果的 sid)"}}, ["sid"]),
         lambda a: client(acct(a)).book_detail(a["sid"])),

        ("book_holdings",
         "館藏狀態：某書目在各分館的館藏/可借狀態。",
         obj({"sid": {"type": "integer", "description": "書目記錄 id"}}, ["sid"]),
         lambda a: client(acct(a)).holdings(a["sid"])),

        ("my_current_loans",
         "借閱中：目前借出的書籍清單。",
         obj(),
         lambda a: client(acct(a)).current_loans()),

        ("my_borrow_history",
         "借閱歷史：過去借閱紀錄。",
         obj(),
         lambda a: client(acct(a)).borrow_history()),

        ("my_reservations",
         "預約清單。arrived=false 為排隊中尚未到館；arrived=true 為已到可取書。",
         obj({"arrived": {"type": "boolean", "description": "是否只列已到館 (預設 false)"}}),
         lambda a: client(acct(a)).reservations(a.get("arrived", False))),

        ("my_cross_borrow",
         "通閱 (跨館通閱 / ILL)：進行中的通閱項目。",
         obj(),
         lambda a: client(acct(a)).cross_borrow_list()),

        ("list_sites",
         "分館 / 取書地點清單 (keepSiteId + keepsiteCode)。預約取書點需用 keepSiteId。",
         obj(),
         lambda a: client(acct(a)).sites()),

        ("reader_info",
         "讀者資訊 (登入後的 reader_code、姓名等)。",
         obj(),
         lambda a: {"reader_code": client(acct(a)).reader_code,
                    "detail": client(acct(a)).reader_info()}),

        ("place_hold",
         "預約下單 (寫入動作，會真的預約)。需 holdId、pickupKeepSiteId(見 list_sites)、readerValidDate(借書證到期日 YYYY-MM-DD)。",
         obj({
             "holdId": {"type": "integer"},
             "pickupKeepSiteId": {"type": "integer"},
             "readerValidDate": {"type": "string", "description": "借書證到期日 YYYY-MM-DD"},
         }, ["holdId", "pickupKeepSiteId", "readerValidDate"]),
         lambda a: client(acct(a)).place_hold(a["holdId"], a["pickupKeepSiteId"], a["readerValidDate"])),

        ("cancel_reservation",
         "取消預約 (寫入動作)。以預約 id 取消。",
         obj({"reserveId": {"type": "integer"}}, ["reserveId"]),
         lambda a: client(acct(a)).cancel_reservation(a["reserveId"])),

        ("renew_books",
         "續借 (寫入動作)。以借閱 id (借閱中書籍的 len_id) 陣列續借。",
         obj({"ids": {"type": "array", "items": {"type": "integer"}}}, ["ids"]),
         lambda a: client(acct(a)).renew_books(a["ids"])),
    ]


TOOLS = _tools()
HANDLERS = {name: fn for name, _d, _s, fn in TOOLS}


def _tool_list_payload():
    return [{"name": n, "description": d, "inputSchema": s} for n, d, s, _fn in TOOLS]


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "taolib", "version": "1.0.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tool_list_payload()}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "unknown tool: %s" % name}}
        try:
            result = fn(args)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}]}}
        except TaoLibError as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "TaoLibError: " + str(e)}],
                "isError": True}}
        except Exception as e:  # noqa: BLE001 - surface to agent, keep server alive
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "Error: %s\n%s" % (e, traceback.format_exc())}],
                "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found: %s" % method}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
