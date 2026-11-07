"""uv run python -m simulator [--port 8001]: open http://127.0.0.1:8001 in a browser."""

import argparse
import logging

import uvicorn

from simulator.web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("ocpp").setLevel(logging.WARNING)  # every frame is on the page already
    print(f"Charger simulator: http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
