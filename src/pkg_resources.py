"""Minimal compatibility shim for packages still importing pkg_resources."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import Distribution, distributions, version


@dataclass(frozen=True)
class _Distribution:
    key: str
    version: str


def get_distribution(name: str) -> _Distribution:
    return _Distribution(key=name, version=version(name))


def _to_legacy(dist: Distribution) -> _Distribution:
    meta_name = dist.metadata.get("Name", dist.name or "unknown")
    return _Distribution(
        key=(meta_name or "unknown").lower(),
        version=dist.version,
    )


working_set = [_to_legacy(d) for d in distributions()]
