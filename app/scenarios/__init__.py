"""Scenarios belief engine — see docs/scenarios/01-foundation.md.

Stage 1 surface:
  posterior.compute_posterior — pure Bayesian update, no DB
  posterior.compute_scenario_probabilities — derive P(scenario) under independence
  posterior.compute_sensitivity — numerical ∂P(scenario)/∂P(predicate=state)
  service.recompute_predicate / recompute_all — DB-aware orchestration
  integrity.validate_seed — sanity-check the seeded predicate/scenario rows
"""
