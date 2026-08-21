"""
Which couriers exist, loaded from config rather than compiled in.

The point of the whole contract: adding the tenth courier is a JSON file, not
a release. So the registry reads `config/couriers/*.json` at start-up, builds
each provider's lookup index once, and refuses to start on a broken mapping.

**Refuses to start**, rather than skipping the bad file and carrying on. A
courier whose mapping cannot express DELIVERED accepts every callback politely
and moves nothing: parcels sit dispatched forever while sellers wait to be
paid, and no error appears anywhere. A service that will not start is a
five-minute outage during a deploy; a service that runs blind is a week of
unpaid sellers nobody has noticed.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from .courier_rules import CourierStatus, build_index, validate_mapping

logger = logging.getLogger(__name__)

# Mounted into the container. Outside the image on purpose: a courier mapping
# changes when a courier changes their API, which has nothing to do with a
# release of this service.
COURIER_CONFIG_DIR = Path(os.getenv("COURIER_CONFIG_DIR", "/app/config/couriers"))


class Courier:
    def __init__(self, provider: str, display_name: str,
                 statuses: Dict[str, List[str]]):
        self.provider = provider
        self.display_name = display_name or provider
        self.statuses = statuses
        self.index = build_index(statuses)

    def canonical(self, raw_status: Optional[str]) -> Optional[CourierStatus]:
        from .courier_rules import map_status
        return map_status(self.index, raw_status)


_registry: Dict[str, Courier] = {}


def load(directory: Path = None) -> Dict[str, Courier]:
    """Read every courier file, or raise with everything that is wrong."""
    directory = directory or COURIER_CONFIG_DIR
    registry: Dict[str, Courier] = {}
    problems: List[str] = []

    if not directory.exists():
        raise RuntimeError(
            f"no courier configuration at {directory}. Every parcel needs a "
            f"provider; refusing to start with none.")

    for path in sorted(directory.glob("*.json")):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"{path.name}: not readable as JSON ({e})")
            continue

        provider = (config.get("provider") or "").strip()
        if not provider:
            problems.append(f"{path.name}: no provider slug")
            continue

        found = validate_mapping(provider, config.get("statuses"))
        if found:
            problems.extend(f"{path.name}: {p.detail}" for p in found)
            continue

        registry[provider] = Courier(provider, config.get("display_name"),
                                     config["statuses"])

    if problems:
        raise RuntimeError(
            "courier configuration is invalid, refusing to start:\n  - "
            + "\n  - ".join(problems))

    if not registry:
        raise RuntimeError(f"no courier mappings found in {directory}")

    logger.info("couriers loaded: %s", ", ".join(sorted(registry)))
    return registry


def init(directory: Path = None) -> None:
    global _registry
    _registry = load(directory)


def get(provider: str) -> Optional[Courier]:
    return _registry.get((provider or "").strip().lower())


def known() -> List[str]:
    return sorted(_registry)
