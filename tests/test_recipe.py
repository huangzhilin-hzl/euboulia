from euboulia.campaign import CampaignRunResult, CampaignSafetyError
from euboulia.config import CampaignConfig, load_config
from euboulia.recipe import (
    RecipeConfig,
    RecipeRunResult,
    RecipeSafetyError,
    load_recipe,
)


def test_recipe_api_is_a_non_breaking_public_vocabulary() -> None:
    assert RecipeConfig is CampaignConfig
    assert RecipeRunResult is CampaignRunResult
    assert RecipeSafetyError is CampaignSafetyError
    assert load_recipe is load_config
