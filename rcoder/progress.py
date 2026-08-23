"""One self-editing status message per task.

Borrowed wholesale from MusicLore's StatusMessage, for the reason the owner
gave there: the terminal always had better feedback than the phone, and
minutes of silence are indistinguishable from a dead bot. Telegram edits
never notify, so a whole run costs exactly one buzz.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Telegram rejects >4096 chars and rate-limits rapid edits on the same message.
MAX_CHARS = 3500
MIN_EDIT_INTERVAL = 2.0


class Progress:
    def __init__(self, bot, chat_id: int, header: str) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._header = header
        self._lines: list[str] = []
        self._message = None
        self._last_edit = 0.0
        self._dirty = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._message = await self._bot.send_message(
            chat_id=self._chat_id, text=f"{self._header}\nstarting..."
        )

    def add(self, line: str) -> None:
        self._lines.append(line)
        self._dirty = True

    def _render(self) -> str:
        body = "\n".join(self._lines[-40:])
        text = f"{self._header}\n{body}" if body else self._header
        if len(text) > MAX_CHARS:
            text = text[-MAX_CHARS:]
            text = "...\n" + text[text.index("\n") + 1:]
        return text

    async def flush(self, *, force: bool = False) -> None:
        """Edit the message if enough time has passed, or if forced.

        Throttled because a fast agent emits steps faster than Telegram will
        accept edits, and a 429 in the middle of a run must never kill it.
        """
        if self._message is None or (not self._dirty and not force):
            return
        now = time.monotonic()
        if not force and now - self._last_edit < MIN_EDIT_INTERVAL:
            return
        self._last_edit = now
        self._dirty = False
        async with self._lock:
            try:
                await self._message.edit_text(self._render())
            except Exception as exc:  # noqa: BLE001 - progress must never fail a run
                logger.debug("Progress edit failed (ignored): %s", exc)

    async def finish(self, final_line: str | None = None) -> None:
        if final_line:
            self.add(final_line)
        await self.flush(force=True)
