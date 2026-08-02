"""Set or clear the info banner shown to every mobile client on launch.

Bypasses the superuser-guarded API entirely — useful for pushing/pulling the
announcement from a terminal without opening Swagger or minting a JWT. Talks to
Redis directly, the same way `seed_dev_data.py` talks to Postgres directly: no
FastAPI request lifecycle involved.

Usage (inside the backend container):
    docker compose exec backend python set_banner.py "maintenance downtime Friday 10pm-midnight"
    docker compose exec backend python set_banner.py "maintenance downtime Friday 10pm-midnight" --de "Wartungsausfall Freitag 22-24 Uhr"
    docker compose exec backend python set_banner.py --clear
"""

import argparse
import asyncio

from app.core.banner import clear_banner, set_banner
from app.redis import redis_client


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "message", nargs="?", help="The English announcement text to push."
    )
    parser.add_argument("--de", help="The German announcement text (optional).")
    parser.add_argument(
        "--clear", action="store_true", help="Remove the current banner."
    )
    args = parser.parse_args()

    if args.clear:
        await clear_banner(redis_client)
        print("Banner cleared.")
        return

    if not args.message:
        parser.error("provide a message, or pass --clear")

    messages = {"en": args.message}
    if args.de:
        messages["de"] = args.de

    banner = await set_banner(redis_client, messages)
    print(f"Banner set (id={banner['id']}): {banner['messages']}")


if __name__ == "__main__":
    asyncio.run(main())
