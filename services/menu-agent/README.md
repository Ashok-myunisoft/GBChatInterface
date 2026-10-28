# GoodBooks Menu Creation Agent

MCP server (GB4 tools) + OpenAI tool-calling client + FastAPI wrapper,
so this can be run locally and tested through Swagger UI like the Task
bot.

## Structure

```
config/
  settings.py          # env-driven config, no hardcoded values
  menu_enums.json       # Type/Nature/Status options -- see "must verify" below
services/
  envelope.py           # unwraps GB4's WCF-style {Id,Body,OUId,...} responses
  client.py              # HTTP + auth (login is best-effort, see below)
  picklists.py            # all dropdown/lookup calls (all POST, confirmed)
  code_generator.py        # menu code generation (fixed, see below)
  save_menu.py              # builds + sends the save payload
  enums.py                   # loads menu_enums.json + verification gate
prompts/
  menu_creation_prompt.md      # minimal-input flow: agent resolves everything
mcp_server.py                   # FastMCP server, run standalone or via app.py
mcp_client.py                    # OpenAI + MCP stdio client
app.py                             # FastAPI wrapper -- run this
```

## Setup

```bash
pip install -r requirements.txt
```

No `.env` file needed — nothing is read from disk. Credentials are
given at runtime instead (same idea as the Task bot's login string).

## Run

```bash
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000/docs`. First, log in with `POST /connect`:
```json
{
  "goodbooks_base_url": "http://<server-ip>:<port>",
  "goodbooks_db_name": "USCDEVSYS",
  "goodbooks_user_code": "your_user_code",
  "goodbooks_password": "your_password",
  "openai_api_key": "sk-...",
  "openai_model": "gpt-4o-mini"
}
```
This starts the MCP server subprocess with those GoodBooks creds passed
as its environment (never written to a file, never touches this
process's own env) and confirms the OpenAI key by loading the tool
list. `tools_loaded` in the response should be > 0.

Then try `POST /chat` with:
```json
{"session_id": "test", "message": "create a menu called Vendor Report for module Purchase"}
```

Credentials live in memory only for the life of the running process —
restart the server (or call `/connect` again) to change them. If
`/chat` is called before `/connect`, it returns a 400 telling you to
connect first. Any unhandled error now comes back as JSON
`{"error": "<ExceptionType>", "detail": "<message>"}` instead of a
bare 500, so you can see what actually broke without digging through
the console.

## What changed from the first draft, and why

1. **Login was unimplemented** (`raise NotImplementedError`) — now has
   a real (but unverified, see below) implementation.
2. **Code generation was fundamentally wrong** — it assumed a menu's
   code prefix matches the module's own Code (e.g. `INVEN00042`). Real
   HAR data proved every module uses its own arbitrary historical
   prefix (`INVEN` -> `ST`, `HRMS` -> `PL`, `FINANCE` -> `AC`). Fixed
   to read the module's actual existing codes and continue that
   sequence instead of guessing.
3. **Most lookup calls used GET** — confirmed from HAR that every
   SelectList endpoint is POST. Fixed.
4. **`default_image()` guessed a "default" image by keyword** —
   removed; defaults to NONE/-1 instead, since we have no confirmed
   business rule for this.
5. **The prompt asked the user for nearly every field** — directly
   contradicted the actual requirement ("user only says menu name +
   module name, agent fills the rest"). Rewritten to match that.
6. **No FastAPI/HTTP layer at all** — added `app.py` so this can be
   run and tested the same way as the Task bot.
7. **Response unwrapping was missing** — GB4 wraps every response in
   `{"Id":.., "Body": <string>, "OUId":.., "ValidationBody":..,
   "Status":..}`; confirmed against real captured responses (login
   endpoints, Module.svc/SelectList, and menu.js's own
   `saveMenuResult` callback reading `data.Body`). Added
   `services/envelope.py` to handle it.

## Still open — do not skip these

- **`client.py::login()` is best-effort, not verified.** We have the
  endpoint *names* (`Server.svc/AuthenticationKey`,
  `User.svc/Authenticate`) from the HAR, but never captured a response
  body for either call. Capture one fresh HAR of the login screen —
  submit with Preserve Log on — and update `login()` to match the
  real request/response shape before relying on this.
- **`config/menu_enums.json` is unverified.** `_verified` is `false`,
  and `create_menu` will refuse to run until you flip it to `true`.
  Verify by reading the real `<select id="MenuType">` /
  `MenuNature` / `MenuStatus` options straight from `menu.php`'s
  source (Sources tab, same technique used to find `saveMenu()`).
- **`MenuCreatedOn`/`MenuModifiedOn` epoch unit** (ms vs seconds) is
  assumed, not confirmed from a real save payload.
- **Save response shape** (does it return the new `MenuId`?) is
  unconfirmed — `saveMenuResult` in menu.js only shows a success
  message (`data.Body`), not the created record.
- **`get_sections()`'s exact criteria body** for `GCM.svc/SelectList`
  wasn't captured in full detail — first thing to check if this call
  errors or returns nothing.

None of these block you from wiring the whole thing together and
testing the flow up to the point of an actual save — but don't trust
a real save to GB4 until the login flow and enums file are verified
against real captures.
