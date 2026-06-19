from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Literal

from config import settings


@dataclass
class ContextMessage:
    role: Literal["user", "model"]
    content: str


class ContextManager:
    """Per-channel sliding-window conversation history (in-memory)."""

    def __init__(self):
        self._histories: dict[int, deque[ContextMessage]] = defaultdict(
            lambda: deque(maxlen=settings.context_window)
        )

    def add_user(self, channel_id: int, content: str, display_name: str = ""):
        text = f"{display_name}: {content}" if display_name else content
        self._histories[channel_id].append(ContextMessage(role="user", content=text))

    def add_bot(self, channel_id: int, content: str):
        self._histories[channel_id].append(ContextMessage(role="model", content=content))

    def get(self, channel_id: int) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in self._histories[channel_id]]

    def clear(self, channel_id: int):
        self._histories[channel_id].clear()


context_manager = ContextManager()
