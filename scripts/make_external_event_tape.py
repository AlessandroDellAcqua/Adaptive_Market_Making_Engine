"""Generate a persisted Project 1 event tape for later strategy replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lob_sim.external_execution import generate_external_event_tape
from lob_sim.simulation import SimulationConfig
from lob_sim.synthetic import generate_market_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulator-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "Limit_Order_Book_Simulator",
        help="Project 1 repository root or its src directory",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--regime", default="liquid")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/external_lob/event_tape_seed0.json"),
    )
    args = parser.parse_args()

    config = SimulationConfig(
        horizon_seconds=20.0,
        dt_seconds=0.1,
        volatility_ticks=5.0,
        half_spread_ticks=2,
        depth=10,
        q_max=10,
    )
    path = generate_market_path(config, args.seed)
    tape = generate_external_event_tape(
        args.simulator_root,
        path,
        seed=args.seed,
        order_flow_config={"regime": args.regime},
    )
    tape.write(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "digest": tape.digest,
                "events": len(tape.events),
                "reference_points": len(tape.market_path.points),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
