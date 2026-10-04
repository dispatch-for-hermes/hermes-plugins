# Dispatch plugins for Hermes

Plugins that connect a [Hermes](https://github.com/NousResearch/hermes-agent) gateway to **Dispatch**, the iPhone
app for Hermes.

## dispatch-push: lock-screen notifications

When a bot finishes a reply, or needs your approval to run a command, Dispatch shows it on your lock screen, even
when the app isn't open. Approvals carry **Approve once** and **Deny** buttons that work from the lock screen.

Without this plugin, Dispatch can only alert you while it's open, or for about 20 seconds after you leave it.

### Install

On the computer that runs your Hermes gateway:

```bash
hermes plugins install dispatch-for-hermes/hermes-plugins/dispatch-push
```

```bash
hermes plugins enable dispatch-push
```

Then restart `hermes serve` (or the Hermes app). The plugin's routes appear only after a restart. Open Dispatch and
make sure **Settings › Notifications** is on. The next time Dispatch signs in to your gateway, it registers for push.

Or ask your Hermes bot to do it: *"Install the Dispatch push plugin: `hermes plugins install dispatch-for-hermes/hermes-plugins/dispatch-push`,
enable it, then tell me to restart hermes serve."*

Hermes calls this a custom, unreviewed source when you install it. Its security scan may point out the one `curl` call
in `push.py`. That call is used only by the app's developer, who sends to Apple directly. Everyone else goes through the
relay described below.

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

## License

MIT
