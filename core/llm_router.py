from config import settings
from core.message_parser import ComplexityResult, ModelTier


class LLMRouter:
    """Maps a ComplexityResult to the appropriate Gemini model name."""

    _TIER_TO_MODEL: dict[ModelTier, str] = {
        ModelTier.LOW:    settings.model_low,
        ModelTier.MEDIUM: settings.model_medium,
        ModelTier.HIGH:   settings.model_high,
    }

    def select_model(self, result: ComplexityResult) -> str:
        return self._TIER_TO_MODEL[result.tier]

    def describe(self, result: ComplexityResult) -> str:
        return f"[router] {result.summary} → {self.select_model(result)}"


router = LLMRouter()
