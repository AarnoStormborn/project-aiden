"""Token estimation for the compaction trigger.

The estimator reads the parts it can see — text, thinking, tool arguments, tool results — and
divides by ``CHARS_PER_TOKEN``. It does not model the wire format, which is why every rounding goes
up and why ``MESSAGE_OVERHEAD_TOKENS`` exists: a message contributes role and markup tokens that
carry no characters of their own.
"""

from __future__ import annotations

from .. import config
from ..providers.types import (
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)

#: Role and markup tokens per message, which carry no characters. Deliberately coarse and rounded
#: up: this estimator only has to be right about "near the window", and under-counting is the
#: dangerous direction — an overflowing request fails the whole turn, an early compaction costs one
#: summary call.
MESSAGE_OVERHEAD_TOKENS = 4


def part_chars(part: object) -> int:
    """Characters a part will contribute to the request, as far as we can know them."""
    if isinstance(part, TextPart):
        return len(part.text)
    if isinstance(part, ThinkingPart):
        # Thinking is sent back on some providers and dropped on others; counting it is the safe
        # direction, and it is bounded by the same budget as the visible text.
        return len(part.text)
    if isinstance(part, ToolCallPart):
        return len(part.name) + len(part.raw_arguments or str(part.arguments))
    if isinstance(part, ToolResultPart):
        return len(part.output)
    return 0


def message_chars(message: Message) -> int:
    return sum(part_chars(part) for part in message.content)


def estimate_text(text: str) -> int:
    """Tokens for a bare string, rounded up. Used for system prompts and summaries."""
    return _tokens(len(text))


def estimate_tokens(messages: list[Message], system: str = "") -> int:
    """Estimated tokens for one request: the system prompt plus every message.

    Rounded up at the end, so a long run of small parts still pays for its message overhead.
    """
    chars = len(system)
    for message in messages:
        chars += message_chars(message)
    return _tokens(chars) + MESSAGE_OVERHEAD_TOKENS * len(messages)


def _tokens(chars: int) -> int:
    # Ceiling division: 1 char is 1 token, not 0, so a request is never counted as free.
    return -(-chars // config.CHARS_PER_TOKEN)


def should_compact(
    estimated_tokens: int,
    context_window: int,
    reserve_tokens: int = config.RESERVE_TOKENS,
) -> bool:
    """Whether the next request would come too close to the model's window.

    ``reserve_tokens`` is the room the reply needs: without it the request fits and the *answer* is
    what overflows, which surfaces as a truncated turn rather than a clean compaction.
    """
    if context_window <= 0:
        # An unknown window is treated as unlimited rather than as zero: a catalog entry without a
        # contextWindow should not make every run compact on its first turn.
        return False
    return estimated_tokens > context_window - reserve_tokens
