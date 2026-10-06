# Dispatch plugins for Hermes

Plugins that connect a [Hermes](https://github.com/NousResearch/hermes-agent) gateway to **Dispatch**, the iPhone
app for Hermes.

## dispatch-push: lock-screen notifications

When a bot finishes a reply, or needs your approval to run a command, Dispatch shows it on your lock screen, even
when the app isn't open. Approvals carry **Approve once** and **Deny** buttons that work from the lock screen.

Without this plugin, Dispatch can only alert you while it's open, or for about 20 seconds after you leave it.

### Install

The easy way: open Dispatch. When your gateway doesn't have this plugin, Dispatch offers to have your Hermes bot
install it. Tap **Ask**, and the bot installs it, restarts Hermes, and Dispatch turns notifications on.

To do it yourself, on the computer that runs your Hermes gateway:

```bash
hermes plugins install dispatch-for-hermes/hermes-plugins/dispatch-push --enable
```

Then restart `hermes serve` or `hermes dashboard` (or the Hermes app). The plugin's routes appear only after a restart.
Open Dispatch and make sure **Settings › Notifications** is on. Dispatch registers for push the next time it opens.

To update an older copy, run the same install command with `--force` added. Your registered devices are kept.

### What it sends, and who can read it

Your gateway can't send to Apple itself: only the app's developer holds Dispatch's Apple push key. Alerts go through
the Dispatch push relay (`https://push.getdispatchapp.com`), which holds that key and forwards them to Apple.

**The relay can't read your alerts.** When Dispatch registers, it gives your gateway a key that only your phone holds
(AES-256). The plugin seals every alert with it before it leaves your computer. The relay and Apple carry only
"Dispatch: New notification" and a sealed box, and your phone opens the box. The relay learns your phone's push token,
when an alert was sent, and whether it offers Approve/Deny. It never learns who it's from or what it says. It refuses
anything that isn't sealed, so nobody can use it to put other words on your lock screen.

Approve and Deny go from your phone straight to your gateway, with your normal sign-in, never through the relay.
Each one can only answer the request its alert was sent for.

In Dispatch, **Settings › Notifications › Show Previews** off makes alerts say only who needs you, even inside the seal.

### Settings

All optional, in `~/.hermes/.env`:

| Variable | What it does |
|---|---|
| `DISPATCH_PUSH_RELAY_URL` | Another relay's HTTPS address, or `off` to send nothing |
| `DISPATCH_PUSH_RELAY_KEY` | A key, for a private relay that asks for one |

### Limits

- Only Dispatch's own chats alert (not Telegram, Discord or other messaging platforms, and not cron jobs yet).
- Approvals time out after 60 seconds by default. To answer from the lock screen, raise `approvals.timeout` in
  Hermes' `config.yaml` (300 works well).
- Approvals from the lock screen need Hermes' default, unisolated turns (`dashboard.turn_isolation` off).

### Tests

```bash
python3 -m unittest dispatch-push/test_push.py
```

Tests that need `cryptography` or `fastapi` skip without them.

## dispatch-browser: watch and take over a bot's browser

See the browser a bot is using, live, in Dispatch, and take it over when it needs you: to sign in, enter a code or
solve a CAPTCHA. Bots keep every browser tool Hermes gives them; the plugin overrides nothing. It adds one tool,
`browser_ask_user`, which lets a bot hand you its browser and wait until you give it back.

### Install

The easy way: open **Watch Browser** for a bot in Dispatch. When your gateway doesn't have this plugin, Dispatch offers
to have your Hermes bot install it.

To do it yourself, on the computer that runs your Hermes gateway, install it once, for the main profile:

```bash
hermes plugins install dispatch-for-hermes/hermes-plugins/dispatch-browser --enable
```

Then restart `hermes serve` or `hermes dashboard` (or the Hermes app) and the messaging gateway, if one runs. To update
an older copy, run the same command with `--force` added. Each bot's browser is a headless Google Chrome (or
Chromium), so one of them needs to be installed on that computer.

### Every bot, its own browser

Once the dashboard restarts, the plugin adds itself to every other bot profile, including bots you make later (a
profile that disabled it is left alone), and gives each bot a headless Chrome of its own. Its profile folder is the
bot's `chrome-debug` folder, so sign-ins stay between turns and restarts, and Dispatch can show it any time: open it,
take it over and sign in to sites before the bot needs them. A Chrome that crashes or freezes is restarted, a copied
bot gets its own Chrome instead of sharing its source's, and a deleted bot's Chrome is shut down.

Bots set up another way keep their setup: a Chrome already connected with `/browser connect`, the real-profile Chrome,
Camofox, or a remote debugging address. To give a bot back its previous browser, remove `browser.cdp_url` from its
`config.yaml`; the plugin then leaves that bot alone. `DISPATCH_BROWSER_OWN_CHROME=0` in the dashboard's environment
turns the whole thing off.

### How it works

The bot's browser is never exposed: the plugin reads Chrome's own screencast on this computer and relays it to Dispatch
through your gateway's normal sign-in. While you hold a browser, that bot's browser tools are refused (it can call
`browser_ask_user` to wait for you), and what you tap and type reaches only that browser's page.

## License

MIT
