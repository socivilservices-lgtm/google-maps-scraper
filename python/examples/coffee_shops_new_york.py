"""Search coffee shops in New York and write JSON plus CSV.

Run from the python directory after installing requirements:

    python examples/coffee_shops_new_york.py
"""

import asyncio
from pathlib import Path

from gmaps_service.config import Config
from gmaps_service.scraper import GoogleMapsScraper


async def main() -> None:
    config = Config.load()
    scraper = GoogleMapsScraper(config)
    result = await scraper.run("coffee shops", "New York", max_results=10)
    output = Path(config.output_dir) / "coffee-shops-new-york"
    paths = scraper.save_results(result, output)
    print(f"{result.total_found} businesses via {result.backend}")
    print(paths["json"])
    print(paths["csv"])


if __name__ == "__main__":
    asyncio.run(main())
