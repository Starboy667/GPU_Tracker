# GPU Watch 🇫🇷

Scrapes GPU prices from French retailers and pings Discord when a card drops below your target.

**Supported:** LDLC, Materiel.net, TopAchat, Grosbill, Alternate, **Dealabs** (public RSS feed of the
"cartes graphiques" group, which also surfaces deals from Amazon, Cdiscount, Darty, etc.) and **Idealo**
(lowest price per product across all its shops).

Idealo rejects plain HTTP clients, so it's fetched with `curl_cffi` impersonating Chrome. This goes against
Idealo's terms of use: keep the check interval long (≥ 30 min), or remove `idealo` from `sites:` if it starts
returning 403.

Not supported: LeDénicheur (blocks even browser impersonation), PCPartPicker (prices load via JavaScript),
Amazon.fr and Cdiscount (bot checks). Use LeDénicheur's built-in "Alerte prix" instead.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # paste your Discord webhook URL in it
.venv/bin/python gpu_watch.py --test-discord
```

Discord webhook: Server → channel settings (⚙️) → Intégrations → Webhooks → New webhook → Copy URL.

## Usage

| Command | What it does |
|---|---|
| `gpu_watch.py --list` | Show every matching card, cheapest first, with its usual price (🔥 = deal) |
| `gpu_watch.py --dry-run` | Run a check, print alerts instead of sending |
| `gpu_watch.py` | One check; sends Discord alerts for new deals |
| `gpu_watch.py --loop 30` | Check every ~30 min forever |

Edit `config.yaml` to add GPUs or tweak filters (`match` / `exclude` are regexes on the product title).

## How deals are detected

You don't have to enter target prices. Every run stores, for each GPU, the **lowest price of the day** at which
it was available (in stock or unknown stock, Dealabs excluded) in `price_stats.json` (kept 120 days).
The **usual price** is the median of those daily lows over the last `baseline_days` (14) days, not counting today.
A listing is a deal when it's at least `deal_drop_pct` % (8 %) under the usual price.

- Alerts start once a GPU has `baseline_min_days` (3) days of history. On first run, the stats are seeded from
  `price_history.csv` if it exists.
- `--list` shows the usual price and the deal threshold for each GPU.
- Optional `max_price` on a watch: also alert whenever the price is at or below it, whatever the baseline says.

**No spam:** each product alerts once when it goes under target, again only if it drops another `renotify_drop` €,
and is re-armed if it goes back above the deal threshold or out of stock. Every price seen is logged to `price_history.csv`.

## Running it automatically

- **Simple:** `nohup .venv/bin/python gpu_watch.py --loop 30 >> watch.log 2>&1 &`
- **cron** (on WSL, start cron first: `sudo service cron start`):
  `*/30 * * * * cd /home/julien/workspace/gpu && .venv/bin/python gpu_watch.py >> watch.log 2>&1`

Don't go below ~15 min between checks — be nice to the retailers or you'll get blocked.
