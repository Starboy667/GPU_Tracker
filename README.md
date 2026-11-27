# Price Watch

Watches the price and stock of products in French shops and tells you when one becomes a deal.
It started as a script that tracked graphics cards and pinged Discord; it is now a small web app
that follows any product, behind a login, with alerts on the channels you choose.

- **Any product**: search LDLC, Materiel.net, TopAchat, Grosbill, Alternate, Joybuy, Idealo and
  Dealabs by name, or follow the exact page of a product on any shop that publishes its price in a
  standard way.
- **Video games**: Steam's own price, and every key-shop offer listed by DLCompare (Instant Gaming,
  Eneba, Kinguin, G2A...), filtered by platform, edition and region.
- **No target price needed**: it learns each product's usual price and alerts on a real drop. A
  fixed target price and back-in-stock alerts are there when you want them.
- **Alerts where you read them**: Discord, Telegram, email, generic webhook, Slack (and
  Mattermost / Rocket.Chat), ntfy, Gotify, Pushover, Free Mobile SMS, plus anything
  [Apprise](https://github.com/caronc/apprise/wiki) supports (Matrix, Signal, Teams, WhatsApp, ...).
- **Yours, or shared**: every watch belongs to an account and is private until its owner shares
  it with chosen accounts, with everyone on the server, or with a read-only link that needs no
  account. People who can see a watch can ask for its alerts on their own channels.
- **Self-hosted**: one container, one SQLite file, email + password accounts.
- **A front door**: visitors without an account get a public page that explains what the server
  does, with the products floating in 3D. Everything else stays behind the login.

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
| `OUTBOUND_CHECK_URL` | | A "what is my address" JSON endpoint, e.g. `https://am.i.mullvad.net/json`. When set, the Status page shows the address the shops see and whether it is a VPN exit. |
| `ACCESS_REQUEST_URL` | | Where the public home page sends people who want an account: an `https://` page or a `mailto:` link. Empty: the page only says to ask whoever runs the server. |

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
- A shop that does not answer at all, or answers 403/429, is asked once per check: the other
  watches skip it instead of waiting for the same time-out.

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

- **Joybuy** (JD.com's European shop) is a general marketplace: its results are read from the data
  its search page embeds for its own rendering.
- **Steam** is read through the JSON endpoints of its store (French prices). Searching a name
  returns the game alone when a result bears exactly that name; otherwise give the store page
  (`https://store.steampowered.com/app/1245620/ELDEN_RING/`) so DLC and sequels stay out.
- **DLCompare** lists hundreds of offers per game. Each becomes a line such as
  `Elden Ring - Steam (PC) - Standard Edition - Europe - code SEAL17DLC`, with `[DLC]` or `[extra]`
  after the name when it is not the game itself, `Vente de compte` for accounts, `boîte` for boxed
  copies. The "Video game (PC)" preset filters consoles, accounts, gifts, boxes, DLC and
  "Rest of World" keys out; edit its exclude pattern to let some back in. Prices include the
  shop's payment fees; a `code` in the title is the coupon that price assumes. Like Idealo, this
  goes against the site's terms: keep the interval long. The game's page address is more exact
  than its name.
- Not supported: GoCléCD / AllKeyShop (answers 429 to automated requests) and Instant Gaming's
  search (rendered in the browser; its product pages work with the product-page tracker from a
  home connection, not from every server address).

Do not go below about 15 minutes between checks: the shops will block you.

## The public home page

`/` answers everybody. Signed in, it is your watches. Signed out, it is a page that says what Price
Watch does: which shops this server reads and how often, how a deal is detected, where alerts go.
It shows nothing about accounts or watches, sets no cookie, carries no version number and asks
search engines to stay away (`noindex`).

The products on it are a [three.js](https://threejs.org) scene. It is decoration: the page says
the same thing without it, and falls back to a still poster when WebGL is missing, when the
browser draws 3D on the processor, when the visitor asked to save data, or when frames come too
slowly even at the lowest quality. With "reduce motion" on, nothing floats and scrolling cuts from
one picture to the next. `?scene=off` and `?scene=on` force either way.

Pages only run scripts and load fonts from the app itself (`Content-Security-Policy`), so both are
committed rather than fetched from a CDN:

- `pricewatch/web/static/vendor/three-<version>.min.js` is built by `tools/vendor-three.sh` from
  the npm package (MIT licence), with only what `tools/vendor-three-entry.js` lists.
- `pricewatch/web/static/fonts/archivo-latin.woff2` is [Archivo](https://github.com/Omnibus-Type/Archivo)
  (SIL Open Font License, copied next to it).

Static files are served under `/static/v-<hash>/...`, a name that changes with every release, so
browsers keep them for good; text files are compressed, pages never are.

## Accounts and security

- Accounts are created by an administrator (**Settings > Manage accounts**) or from the shell;
  there is no public sign-up and no email recovery. Administrators manage settings and accounts.
  They do not see other people's private watches or anybody's channels.
- **Watches** belong to whoever created them. Only the owner changes, pauses, deletes or shares
  one. Visibility is *private* (the default: nobody else knows it exists), *shared* (with the
  accounts picked, a link, or both) or *public* (every account). The link (`/s/<random>`) shows
  prices, chart and offers, read-only, without an account; replacing it or making the watch
  private kills the old address at once. The link shows prices, not the owner's alert target.
  Only an administrator can move a watch to another account (their own or a public one).
- "Check now" on the home page checks your own watches; for an administrator, everyone's.
- **Channels** belong to one account: nobody else sees, uses or tests them. A watch alerts its
  owner's channels, plus those of every account that follows it and can still see it.
- Scraper alerts about a shop go to every channel that asked for them; those about the host of a
  product page only to administrators, since the host says what somebody is watching.
- Deleting an account deletes its watches, their history and its channels.
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

```bash
python -m pricewatch create-user someone@example.org [--admin]
python -m pricewatch reset-password someone@example.org
python -m pricewatch import examples/gpu-watches.yaml [--owner someone@example.org]
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
  access.py     who may see and change what (every route goes through it)
  web/          FastAPI app, routes, Jinja templates
    static/     tokens.css (typeface, brand colours), app.css + app.js (the app),
                landing.css + landing/*.js (the public page and its 3D scene)
```

Adding a shop: write `pricewatch/scrapers/<shop>.py` with `parse(html)` and `search(ctx, query, url)`,
register it in `pricewatch/scrapers/__init__.py`, and save a trimmed page under `tests/fixtures/`
(`tools/make_fixtures.py` does it). Adding a channel: one module in `pricewatch/notify/` declaring
its fields and a `send(config, message)`; the form is generated from the fields.

`PRODUCT.md` says who the product is for and how it should look and sound; read it before
touching the interface. Every signed-in screen is built the same way: the blue bar, a manila
band holding the breadcrumb and the page's one title (`{% block masthead %}`), then sections
that each open under a line of ink. Prices that matter are printed with the `tag_price` macro,
and the price tag itself (`.tag`) is kept for deals and for choosing what to watch.

## Tests

```bash
.venv/bin/python -m pytest --cov=pricewatch
```

No test touches the network: shop parsers run against saved pages, channels against mocked HTTP.

## Deploying

Behind a VPN (for instance a [gluetun](https://github.com/qdm12/gluetun) container sharing the
app's network), set `OUTBOUND_CHECK_URL` to see on the Status page which address the shops see.
Expect some shops to refuse VPN addresses: they show up as failing on that same page.

The image runs as a non-root user, needs a writable `/data` and nothing else. Run **one** replica:
the scheduler, the sign-in throttling and SQLite all assume a single process. Kubernetes manifests
for the author's cluster live in the `infra-cluster` repository under `manifests/pricewatch/`.
