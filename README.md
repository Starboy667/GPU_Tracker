# Price Watch

Watches the price and stock of products in French shops and tells you when one becomes a deal.
It started as a script that tracked graphics cards and pinged Discord; it is now a small web app
that follows any product, behind a login, with alerts on the channels you choose.

- **Any product**: search LDLC, Materiel.net, TopAchat, Grosbill, Alternate, Idealo and Dealabs by
  name, or follow the exact page of a product on any shop that publishes its price in a standard way.
- **No target price needed**: it learns each product's usual price and alerts on a real drop. A
  fixed target price and back-in-stock alerts are there when you want them.
- **Alerts where you read them**: Discord, Telegram, email, generic webhook, Slack (and
  Mattermost / Rocket.Chat), ntfy, Gotify, Pushover, Free Mobile SMS, plus anything
  [Apprise](https://github.com/caronc/apprise/wiki) supports (Matrix, Signal, Teams, WhatsApp, ...).
- **Self-hosted**: one container, one SQLite file, email + password accounts.

## Run it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env            # set SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD
set -a; . ./.env; set +a
.venv/bin/python -m pricewatch  # http://localhost:8000
```

Or with Docker:

```bash
docker build -t pricewatch .
docker run --rm -p 8000:8000 -v pricewatch-data:/data \
  -e SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  -e ADMIN_EMAIL=you@example.org -e ADMIN_PASSWORD='a long password' \
  -e BASE_URL=http://localhost:8000 pricewatch
```

Sign in, add a notification channel and send yourself a test, then add a watch. To bring over the
original list of graphics cards, paste `examples/gpu-watches.yaml` into **Watches > Import**.

## Configuration

Environment variables, read once at startup.

| Variable | Default | |
|---|---|---|
| `SECRET_KEY` | required | At least 32 random characters. Signs sessions and encrypts stored channel credentials. Changing it signs everyone out and makes saved channels unreadable. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | | First account, created only while no account exists. Password of 10 characters or more. |
| `BASE_URL` | | Public address, used in notification links. An `http://` address turns off Secure cookies (local use). |
| `DATA_DIR` | `data` | Where `pricewatch.db` lives. |
| `APP_TIMEZONE` | `Europe/Paris` | Which day a price belongs to, and displayed times. |
| `PORT`, `HOST` | `8000`, `0.0.0.0` | |
| `SCHEDULER_ENABLED` | `true` | `false` stops the automatic checks; "Check now" still works. |
| `ALLOW_PRIVATE_NOTIFY_URLS` | `false` | `true` lets channels target private or in-cluster hosts (a self-hosted ntfy or Gotify). |

Everything else (check interval, drop percentage, history length, ...) is under **Settings** in the app.

## How deals are detected

Every check stores, for each watch, the **lowest price of the day** at which the product was
available (in stock or unknown stock; Dealabs posts are left out, they are already deals). The
**usual price** is the median of those daily lows over the last 14 days, today excluded, so a
running promotion cannot lower its own reference. An offer is a deal when it is at least 8 % under
the usual price, or at or under the watch's target price if it has one.

- Alerts based on the usual price start after 3 days of history. A target price works from the first check.
- Each offer alerts once, again only if it drops a further 2 %, and is re-armed when it goes back
  above the threshold or out of stock.
- A watch can also alert when a product comes back in stock, whatever the price.
- If a shop fails 6 scheduled checks in a row you get one "scraper" alert, and one more when it works again.

All of these numbers are settings; the drop percentage and the stock rule can differ per watch.

## Watches

A watch searches the selected shops with its name and keeps the offers whose **title matches**.
The match is built from the name (the same words in order, spaces optional; for graphics cards
`RTX 5070` also refuses the Ti / Super / XT variants) and can be replaced by your own regular
expression. An exclude pattern and a minimum price keep accessories, bundles and used items out.
**Test without saving** in the editor shows what each shop returns and why offers are filtered out.

Notes on the sources:

- **TopAchat**'s search returns few products: give it a category page with the right filter.
- **Dealabs** has no search feed. Give it a group feed such as
  `https://www.dealabs.com/rss/groupe/televiseurs`; the default feed of all new deals only reaches
  back about half an hour. The graphics card and TV presets already do.
- **Idealo** rejects plain HTTP clients, so it is fetched with a Chrome fingerprint (`curl_cffi`).
  That goes against Idealo's terms of use: keep the interval at 30 minutes or more, or untick it.
- **Product pages** are read from schema.org data (JSON-LD), price meta tags, or a CSS selector
  you add after the address (`https://www.amazon.fr/dp/... | .a-price .a-offscreen`). Tested from a
  home connection: LDLC, Materiel.net, Boulanger, Rue du Commerce and Son-Vidéo work; Amazon needs
  the selector; Darty, Fnac and Cdiscount block it, follow them through Idealo instead.

Do not go below about 15 minutes between checks: the shops will block you.

## Accounts and security

- Accounts are created by an administrator (**Settings > Manage accounts**) or from the shell;
  there is no public sign-up and no email recovery. Everyone signed in shares the same watches and
  channels; only administrators change settings and accounts.
- Passwords are hashed with Argon2id. Sign-in is throttled: 5 failures per client address, and
  50 per account from anywhere, per 15 minutes (so nobody can lock you out by guessing).
  Sessions are signed cookies (HttpOnly, SameSite=Lax, Secure over https); changing a password
  signs out every other session. Every form carries a CSRF token.
- Channel credentials are encrypted in the database with a key derived from `SECRET_KEY` and are
  never displayed again. Changing where a channel sends (its URL, SMTP host or port) requires
  typing its secrets again, so a channel cannot be redirected to collect what is stored.
- Every address the server fetches is typed by a user, so requests to private or reserved
  addresses are refused when the form is saved and again at connection time
  (`pricewatch/netsafe.py`); responses are capped in size and time. Shop overrides must stay on
  the shop's own domain. Title patterns run with a time limit, so a slow one is reported on the
  watch instead of stalling the checks.
- Accounts are for people you trust: there is no per-user separation of watches or channels.

```bash
python -m pricewatch create-user someone@example.org [--admin]
python -m pricewatch reset-password someone@example.org
python -m pricewatch import examples/gpu-watches.yaml
python -m pricewatch check        # one check now, in the foreground
```

## Code map

```
pricewatch/
  scrapers/     one module per shop (pure parse() + search()), product_page.py for any shop
  notify/       one module per channel type, render.py for the message text
  deals.py      usual price, threshold, alert decision: pure functions
  scan.py       scrape one watch          checker.py   one full check: store, detect
  delivery.py   outbox with retries       scheduler.py background loop
  repos/        SQLite access             db.py        schema and migrations
  forms.py      form validation           portability.py  YAML import / export
  netsafe.py    outbound request guard    security.py  passwords, CSRF, throttling, encryption
  web/          FastAPI app, routes, Jinja templates, one CSS file, one small script
```

Adding a shop: write `pricewatch/scrapers/<shop>.py` with `parse(html)` and `search(ctx, query, url)`,
register it in `pricewatch/scrapers/__init__.py`, and save a trimmed page under `tests/fixtures/`
(`tools/make_fixtures.py` does it). Adding a channel: one module in `pricewatch/notify/` declaring
its fields and a `send(config, message)`; the form is generated from the fields.

## Tests

```bash
.venv/bin/python -m pytest --cov=pricewatch
```

No test touches the network: shop parsers run against saved pages, channels against mocked HTTP.

## Deploying

The image runs as a non-root user, needs a writable `/data` and nothing else. Run **one** replica:
the scheduler, the sign-in throttling and SQLite all assume a single process. Kubernetes manifests
for the author's cluster live in the `infra-cluster` repository under `manifests/pricewatch/`.
