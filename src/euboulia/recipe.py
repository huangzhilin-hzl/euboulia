"""Public recipe vocabulary over the legacy campaign implementation.

The aliases keep existing imports and serialized evidence readable while new
integrations can use the clearer recipe terminology.
"""

from euboulia.campaign import (
    CampaignRunResult,
    CampaignSafetyError,
    ExistingResultEvaluation,
    PlannedExperiment,
    evaluate_existing_results,
    plan_campaign,
    run_campaign,
)
from euboulia.config import CampaignConfig, ConfigError, load_config

RecipeConfig = CampaignConfig
RecipeRunResult = CampaignRunResult
RecipeSafetyError = CampaignSafetyError
load_recipe = load_config
plan_recipe = plan_campaign
run_recipe = run_campaign
evaluate_recipe_results = evaluate_existing_results

__all__ = [
    "ConfigError",
    "ExistingResultEvaluation",
    "PlannedExperiment",
    "RecipeConfig",
    "RecipeRunResult",
    "RecipeSafetyError",
    "evaluate_recipe_results",
    "load_recipe",
    "plan_recipe",
    "run_recipe",
]
