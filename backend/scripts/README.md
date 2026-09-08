# Operational scripts

Everything here is run **as a module**, from `/app` (the container's working
directory), never as a file path:

```bash
docker compose exec backend python -m scripts.safe.set_banner --clear
```

`python scripts/safe/set_banner.py` fails with `ModuleNotFoundError: No module named
'app'`. The image installs dependencies with `poetry install --no-root`, so `app` is
importable only because `/app` is on `sys.path` — which it is when Python is given a
module name and the working directory, and is not when Python is given a file path
(that puts the *script's own* directory there instead).

The split is by blast radius, not by how often a script is used.

## `safe/` — no big impact, including on production

Targeted, reversible, or idempotent. These are meant to be run against a live
deployment when needed.

| Script | What it does |
| --- | --- |
| `shell.py` | IPython shell with a sync SQLAlchemy session as `db`. Running it does nothing; what you type does. |
| `set_banner.py` | Sets/clears the launch banner every client sees. One string, reversible with `--clear`. |
| `grant_subscription.py` | Grants/revokes one named user's entitlement (e.g. "supporter"). Reversible with `--revoke`. |
| `backfill_post_media.py` | Re-runs the media pipeline over videos missing a poster frame. Idempotent, and has `--dry-run`. |

## `dangerous/` — never point these at production without meaning it

Each one either destroys data, rewrites shared economy state, fabricates content
real users would see, or forges credentials. Read the module docstring before
running any of them.

| Script | Why it's here |
| --- | --- |
| `reset_content.py` | Deletes **every** post, review and subscription, plus their bucket media. Prompts unless `--yes`. |
| `rebuild_redis.py` | Rebuilds derivable Redis state from Postgres — but reseeds token balances, so it **refunds every token spent** since the last run. Legitimate for reconciliation; not routine. |
| `seed_dev_data.py` | Creates bot users and posts them into every channel. On a live app that is fake content in real feeds. |
| `bulk_create_posts.py` | Creates N real posts via the real route, fanned out to real queues. |
| `pricecheck.py` | Prints live pricing state **and mints a valid JWT for an arbitrary account** — an auth bypass anywhere real. |
| `skew.py` | Writes fabricated congestion and phantom subscribers into live Redis to make per-channel pricing visibly diverge. |
| `cleanup.py` | Undoes `skew.py` by deleting channel subscriber sets and pricing keys — it cannot tell fake members from real ones. |

The Redis-only ones (`skew.py`, `cleanup.py`) are recoverable: Postgres is the source
of truth, so `rebuild_redis.py` restores real membership afterwards. `reset_content.py`
is not recoverable.
