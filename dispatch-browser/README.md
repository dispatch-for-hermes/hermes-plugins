# dispatch-browser

Watch a bot's browser live from Dispatch, and take it over. The bot's browser is the one Hermes already runs
for it (its own Chromium, or the real-profile Chrome when `browser.use_real_profile` is on). Bots keep every
browser tool Hermes gives them; this plugin adds one, `browser_ask_user`, and overrides nothing.

## What the person gets

- **Watch Browser** (bot menu, chat ⋯ menu, Bot Info, and a card above the composer while the bot browses):
  the bot's page, live, following the tab it works in.
- **Take Control**: the bot finishes the browser step it is in, then its browser tools are refused until the
  person taps **Give Back**. Taps, scrolls, typing, Return/Tab/Delete, back/forward/reload and an address bar
  reach the page. Leaving the app to fetch a code keeps the browser held for two minutes.
- **The bot asks**: a bot that hits a sign-in, a code or a CAPTCHA calls `browser_ask_user(reason)`. The card in
  its chat and its row in Bots say what it needs; the call returns when the person gives the browser back
  (or after about six minutes).

Hermes closes its own headless browser when the bot's turn ends, so there is nothing to take over after a bot
has finished; `browser_ask_user` is how a bot keeps its browser open for the person. A bot connected to a
Chrome of its own with `/browser connect` (`browser.cdp_url`, Hermes' `chrome-debug` profile) keeps that
browser, and its sign-ins, between turns: the dashboard lists it from the bot's `browser.cdp_url` while its port
answers, even before the bot has used it, so the person can open it and sign in to sites ahead of time. A claim on
that listing holds back every process that bot runs in (they check the same standing id).

`browser.use_real_profile` copies the person's everyday Chrome profile for the bot. Hermes can't copy it while
that Chrome is open ("profile locked"), so on a Mac where Chrome is always running, `/browser connect` is the
way to give a bot a signed-in browser.

## How it works

- **Agent processes** (the messaging gateway, the dashboard's chat gateway, profile chats, cron) load
  `__init__.py`. `pre_tool_call`/`post_tool_call` watch the browser tools (including Browser Use's
  `browser_exec`) and read Hermes' browser session table, read-only, to find the browser that served the
  call: a Hermes-managed Chromium (its daemon's pid file → Chrome child → `DevToolsActivePort`) or a local
  browser Hermes attached to over CDP (the real-profile Chrome). Browser Use's `browser_exec` on a
  `/browser connect` Chrome or the real-profile Chrome leaves no session entry; those are found from
  `browser.cdp_url` (read without network I/O) or the profile copy's `DevToolsActivePort`, and stay listed
  while their port answers. Cloud browsers, Lightpanda and remote CDP endpoints are not shown. Each browser is published to `<hermes root>/dispatch-browser/browsers/<id>.json`
  (owner-only) with the chat it belongs to and how many browser calls are running in it.
- While `control/<id>.json` exists (written by the dashboard for the person), `pre_tool_call` returns
  `{"action": "block"}` for that browser's tools with a message that points the bot at `browser_ask_user`.
  A request becomes control only when no browser call is running, so the bot is never mid-click.
- **The dashboard** (`hermes serve`) mounts `dashboard/plugin_api.py` at `/api/plugins/dispatch-browser`:

  | Route | |
  |---|---|
  | `GET /health` | `installed`, `protocol` (3), `features`, and `drift` if Hermes changed shape |
  | `GET /sessions?owner=<profile>&session=<id>,<id>` | live browsers with `control` (`bot`/`waiting`/`person`) and `ask` |
  | `WS /activity?owner=<profile>` | the same list, pushed on change (no page images) |
  | `WS /sessions/{id}/watch` | the live view and the person's input (wire format in `stream.py`) |

  Websockets check the dashboard's own credential and Host/Origin rule and never fail open. The Chrome
  endpoint never leaves the gateway.
- **The stream** is Chrome's screencast (JPEG), acknowledged by the phone once drawn; with two frames
  unacknowledged Chrome's own ack is held, so Chrome slows to what the connection carries. Size and quality
  follow the phone's screen and step down when acks come back slowly.

## Depends on Hermes internals

Nothing is patched or wrapped. `agent.py` reads `tools.browser_tool._active_sessions`,
`_last_active_session_key`, `_session_owner_homes`, `_cleanup_lock` and `_socket_safe_tmpdir`, uses
`browser_use_cli._backend_cache_key`, and calls `browser_tool_session._run_browser_command(key, "get", ["url"])`
only while the bot waits in `browser_ask_user` and only while its session exists (it renews Hermes' and the
browser daemon's idle timers). `agent.problems()` checks those shapes at load (written for Hermes 0.21.5); on a
mismatch no hook or tool is registered and `/health` reports the drift.

## Install

```sh
rsync -a --delete --exclude tests --exclude __pycache__ gateway-plugin/dispatch-browser/ ~/.hermes/plugins/dispatch-browser/
hermes plugins enable dispatch-browser
```

Then restart the dashboard and the gateways. Hermes' own Python needs `websockets` and `psutil`; both ship
with Hermes 0.21.5. Earlier Dispatch builds (163–166) shipped a different `dispatch-browser` that replaced the
bot's browser tools with a Chrome extension; installing this one removes that design: also delete
`plugins.entries.dispatch-browser.granted_capabilities` and restore `browser.backend` if that setup changed them.

## Tests

```sh
cd gateway-plugin/dispatch-browser/tests
uv run --no-project --with fastapi --with httpx --with websockets --with uvicorn --with psutil python -m unittest -q test_browser
```

Hermes is faked (its bootstrap rewrites the install). The live tests drive a real headless Chrome on a
throwaway profile through the real routes: watch, take control while a bot call runs, tap, type, Return,
give back, reconnect to a held browser, and the bot's ask on the activity socket.
