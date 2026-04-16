#!/usr/bin/env python3
"""Kleines SimPy-Beispiel: diskrete Ereignissimulation mit Ressource und zwei Kunden.

Voraussetzung: ``pip install simpy`` (in der synarius-core-venv bereits installiert).

Ausführen vom Repo-Root:
  .venv/Scripts/python scripts/simpy_minimal_example.py
"""

from __future__ import annotations

import simpy


def customer(
    env: simpy.Environment,
    name: str,
    counter: simpy.Resource,
    service_time: float,
) -> simpy.events.Process:
    """Kunde wartet auf Bedienplatz, wird bedient, verlässt das System."""

    print(f"t={env.now:5.1f}  {name} kommt")

    with counter.request() as req:
        yield req
        print(f"t={env.now:5.1f}  {name} startet Bedienung")
        yield env.timeout(service_time)

    print(f"t={env.now:5.1f}  {name} fertig")


def main() -> None:
    env = simpy.Environment()
    counter = simpy.Resource(env, capacity=1)

    env.process(customer(env, "A", counter, service_time=3.0))
    env.process(customer(env, "B", counter, service_time=2.0))

    env.run()


if __name__ == "__main__":
    main()
