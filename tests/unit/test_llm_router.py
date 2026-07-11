import pytest
from core.message_parser import ComplexityResult, ModelTier
from core.llm_router import LLMRouter
from config import settings


def make_result(tier: ModelTier) -> ComplexityResult:
    return ComplexityResult(score=0, tier=tier, dimensions={}, detected_intent="general")


@pytest.fixture
def router():
    return LLMRouter()


class TestLLMRouter:
    def test_low_tier_returns_model_low(self, router):
        assert router.select_model(make_result(ModelTier.LOW)) == settings.model_low

    def test_medium_tier_returns_model_medium(self, router):
        assert router.select_model(make_result(ModelTier.MEDIUM)) == settings.model_medium

    def test_high_tier_returns_model_high(self, router):
        assert router.select_model(make_result(ModelTier.HIGH)) == settings.model_high

    def test_describe_contains_tier_and_model(self, router):
        desc = router.describe(make_result(ModelTier.MEDIUM))
        assert "[router]" in desc
        assert "medium" in desc
        assert settings.model_medium in desc
