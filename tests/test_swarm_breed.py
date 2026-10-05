"""Breeding stays inside the recipe space and is reproducible from its seed."""

from __future__ import annotations

import random
import unittest

from agentic_trading.swarm.breed import crossover, immigrant, mutate
from agentic_trading.swarm.recipe import validate


class BreedTests(unittest.TestCase):
    def test_immigrants_are_always_valid_and_reproducible(self) -> None:
        for seed in range(200):
            recipe = immigrant(random.Random(seed))
            self.assertEqual(validate(recipe.to_dict()).id, recipe.id)
            self.assertEqual(recipe.origin, "immigrant")
        self.assertEqual(immigrant(random.Random(5)).id, immigrant(random.Random(5)).id)

    def test_a_mutation_changes_one_or_two_things_and_names_its_parent(self) -> None:
        parent = immigrant(random.Random(1))
        for seed in range(100):
            child = mutate(parent, random.Random(seed))
            self.assertEqual((child.family, child.origin, child.parents), (parent.family, "mutation", (parent.id,)))
            before, after = parent.content(), child.content()
            changed = [k for k in ("universe", "per_order_pct", "inverse_vol") if before[k] != after[k]]
            changed += [k for k in before["params"] if before["params"][k] != after["params"][k]]
            self.assertIn(len(changed), (1, 2))

    def test_crossover_takes_each_gene_from_a_parent(self) -> None:
        rng = random.Random(3)
        a = immigrant(rng)
        b = next(r for r in (immigrant(random.Random(s)) for s in range(500)) if r.family == a.family and r.id != a.id)
        child = crossover(a, b, random.Random(9))
        self.assertEqual((child.origin, child.parents), ("crossover", (a.id, b.id)))
        for key, value in child.content()["params"].items():
            self.assertIn(value, (a.content()["params"][key], b.content()["params"][key]))

    def test_crossing_families_falls_back_to_a_mutation(self) -> None:
        a = next(immigrant(random.Random(s)) for s in range(100) if immigrant(random.Random(s)).family == "trend")
        b = next(immigrant(random.Random(s)) for s in range(100) if immigrant(random.Random(s)).family == "rotation")
        self.assertEqual(crossover(a, b, random.Random(2)).origin, "mutation")
