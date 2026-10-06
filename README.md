# Hermes HQ plugins for Hermes

Plugins that connect a [Hermes](https://github.com/NousResearch/hermes-agent) gateway to **Hermes HQ**, the iPhone
app for Hermes.

## hermes-hq-push: lock-screen notifications

When a bot finishes a reply, or needs your approval to run a command, Hermes HQ shows it on your lock screen, even
when the app isn't open. Approvals carry **Approve once** and **Deny** buttons that work from the lock screen.

Without this plugin, Hermes HQ can only alert you while it's open, or for about 20 seconds after you leave it.

This plugin was called `dispatch-push` before version 0.5. It takes over the old plugin's registered phones on its
first start, and reads the old `DISPATCH_*` settings when the new names aren't set.

### Install

The easy way: open Hermes HQ. When your gateway doesn't have this plugin, or still has `dispatch-push`, Hermes HQ
offers to have your Hermes bot install it. Tap **Ask**, and the bot installs it, removes the old one, restarts Hermes,
and Hermes HQ turns notifications on.

To do it yourself, on the computer that runs your Hermes gateway:

```bash
hermes plugins install mrcharlesiv/hermes-hq-plugins/hermes-hq-push --enable
hermes plugins remove dispatch-push   # only if you had the old plugin
```

Then restart `hermes serve` or `hermes dashboard` (or the Hermes app). The plugin's routes appear only after a restart.
Open Hermes HQ and make sure **Settings › Notifications** is on. Hermes HQ registers for push the next time it opens.

To update an older copy, run the same install command with `--force` added. Your registered devices are kept. If the
old `dispatch-push` is still installed beside it, only `hermes-hq-push` sends alerts, so you never get two.

### What it sends, and who can read it

Your gateway can't send to Apple itself: only the app's developer holds Hermes HQ's Apple push key. Alerts go through
the Hermes HQ push relay (`https://push.getdispatchapp.com`), which holds that key and forwards them to Apple.

**The relay can't read your alerts.** When Hermes HQ registers, it gives your gateway a key that only your phone holds
(AES-256). The plugin seals every alert with it before it leaves your computer. The relay and Apple carry only
"Hermes HQ: New notification" and a sealed box, and your phone opens the box. The relay learns your phone's push token,
when an alert was sent, and whether it offers Approve/Deny. It never learns who it's from or what it says. It refuses
anything that isn't sealed, so nobody can use it to put other words on your lock screen.

Approve and Deny go from your phone straight to your gateway, with your normal sign-in, never through the relay.
Each one can only answer the request its alert was sent for.

In Hermes HQ, **Settings › Notifications › Show Previews** off makes alerts say only who needs you, even inside the seal.

### Settings

All optional, in `~/.hermes/.env` (the old `DISPATCH_PUSH_RELAY_URL` and `DISPATCH_PUSH_RELAY_KEY` still work):

| Variable | What it does |
|---|---|
| `HERMES_HQ_PUSH_RELAY_URL` | Another relay's HTTPS address, or `off` to send nothing |
| `HERMES_HQ_PUSH_RELAY_KEY` | A key, for a private relay that asks for one |

### Limits

- Only Hermes HQ's own chats alert (not Telegram, Discord or other messaging platforms).
- Approvals time out after 60 seconds by default. To answer from the lock screen, raise `approvals.timeout` in
  Hermes' `config.yaml` (300 works well).
- Approvals from the lock screen need Hermes' default, unisolated turns (`dashboard.turn_isolation` off).

### Tests

```bash
python3 -m unittest hermes-hq-push/test_push.py
```

Tests that need `cryptography` or `fastapi` skip without them.

## hermes-hq-browser: watch and take over a bot's browser

See the browser a bot is using, live, in Hermes HQ, and take it over when it needs you: to sign in, enter a code or
solve a CAPTCHA. Bots keep every browser tool Hermes gives them; the plugin overrides nothing. It adds one tool,
`browser_ask_user`, which lets a bot hand you its browser and wait until you give it back.

### Install

The easy way: open **Watch Browser** for a bot in Hermes HQ. When your gateway doesn't have this plugin, Hermes HQ offers
to have your Hermes bot install it.

To do it yourself, on the computer that runs your Hermes gateway. Each bot profile loads its own plugins, so install
it for the main profile and for each other profile (`hermes profile list`):

```bash
hermes plugins install mrcharlesiv/hermes-hq-plugins/hermes-hq-browser --enable
hermes -p <profile> plugins install mrcharlesiv/hermes-hq-plugins/hermes-hq-browser --enable
```

Then restart `hermes serve` or `hermes dashboard` (or the Hermes app) and the messaging gateway, if one runs. To update
an older copy, run the same commands with `--force` added. This plugin was called `dispatch-browser`
before; remove that one first (`hermes plugins remove dispatch-browser`), then check that `browser` is still in any
`platform_toolsets` list in `config.yaml` that had it (removing a plugin with tools can take its toolset out).

### Which browsers it shows

Hermes's own browser for each bot (its Chromium, or the Browser Use harness driving it), a Chrome you connected with
`/browser connect` (`browser.cdp_url` on this computer), and the real-profile Chrome. Not cloud browsers, Lightpanda,
or a remote debugging address.

Hermes closes its own headless browser when the bot's turn ends. A bot connected to a Chrome of its own keeps that
browser, and its sign-ins, between turns. Hermes HQ lists it whenever it's running, so you can open it, take it over
and sign in to sites before the bot needs them.

### How it works

The bot's browser is never exposed: the plugin reads Chrome's own screencast on this computer and relays it to Hermes HQ
through your gateway's normal sign-in. While you hold a browser, that bot's browser tools are refused (it can call
`browser_ask_user` to wait for you), and what you tap and type reaches only that browser's page.

## License

MIT
