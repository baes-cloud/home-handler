# home-handler

A small service that keeps an eye on my home stack (Home Assistant, the NAS, backups, a few servers), has Claude sort out what actually needs me, and sends one note a day instead of pinging me all day.

It runs on a mini PC as a few systemd units. This version only watches and reports: it changes nothing outside its own journal.

## What it watches

- **Devices.** HA devices dropping off more than usual (routers first) or offline for hours, and batteries running down.
- **Space.** Disks projected to fill, from the last week's trend ("full by Sat 5 Dec"), and Music Assistant memory.
- **HA health.** Failover blips, slow restarts, outages, integrations stuck failing to set up, repairs and pending updates.
- **Quiet failures.** Jobs that should have run and didn't: HA and NAS backups, Kometa, a weekly music grab, disk health tests.
- **Watchdog.** Watches the watchers, so it can't quietly die.

## What I get

- **One evening note** to my phone (at most one push a day), written by Claude from what the watchers found.
- **A page** with Waiting on you, Today, Spotted, Watchers and History.
- **A terminal** on my phone's home screen widget ([baeo-home-widget](https://github.com/baes-cloud/baeo-home-widget)): `status`, `waiting`, `spotted`, `done <id>`, `reopen <id>`, or just ask it something. Questions are answered read-only from its own state.

## How it works

1. **Watchers** are plain Python rules. They read from HA (websocket + REST), a homelab status JSON and SSH, and never change anything.
2. Everything goes into a **journal** (SQLite), so the same problem isn't reported twenty times.
3. The **brain** is the Claude CLI run headless with no tools and a daily cap. It sorts the journal into "needs you" and "handled", writes the evening note and answers terminal questions.
4. **Web** serves the page and `POST /api/command` for the widget.

### Away from home (Cloudflare)

The page sits behind a Cloudflare Tunnel and Access, so nothing is port-forwarded. The phone widget reaches `/api/command` with its own Access **service token**: Access only admits that token, and home-handler checks the signed JWT came from it, plus its own command token. On the home LAN it uses the command token alone.

## Run it

```bash
python3 -m home_handler watch --once        # one pass of every watcher
python3 -m home_handler replay --days 10    # what it would have caught
python3 -m home_handler evening --force     # send tonight's note now
python3 -m unittest discover -s tests -t .
```

Units are in `deploy/`. Config overrides go in `~/.config/home_handler/config.toml` (defaults in `home_handler/config.py`). Secrets stay out of the repo:

| Env var | What |
|---|---|
| `HOME_HANDLER_CMD_TOKEN` | Bearer token for the widget's commands |
| `HOME_HANDLER_ACCESS_TEAM` | `https://<team>.cloudflareaccess.com` |
| `HOME_HANDLER_ALLOWED_EMAILS` | Comma-separated emails allowed on the page |
| `HOME_HANDLER_CF_CLIENT_ID` | The widget's Access service token client id |
| `ACCESS_AUD` | The Access app's audience tag |
| `HOME_HANDLER_STATUS` | Path to the homelab status JSON |

The HA token goes in `~/.config/home_handler/ha_token`. The evening note goes through an HA script called `notify_phone`.

It's built around my setup, so treat it as a starting point rather than a drop-in.
