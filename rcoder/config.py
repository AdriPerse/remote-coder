"""Configuration, loaded from .env beside the repo root.

Deliberately the same shape as MusicLore's config: a frozen dataclass built
once, with every failure raising ConfigurationError naming the missing key.
A coding agent that starts up half-configured and discovers it at the first
tool call is a worse failure than one that refuses to start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# Tools whose effects are irreversible enough to be worth a phone buzz.
DEFAULT_INTERRUPT_ON = ("execute", "write_file", "edit_file", "delete")


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def load_env_file(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE lines without overwriting real process variables.

    Process env wins so that MOONSHOT_API_KEY set at user scope (as it
    already is on this machine) keeps working with no .env entry at all.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ and value:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    small_model: str
    reasoning_effort: str
    telegram_token: str
    allowed_chat_id: int
    interrupt_on: tuple[str, ...]
    execute_timeout: int
    scrub_env: tuple[str, ...]
    state_dir: Path = field(default=REPO_ROOT / "state")

    @property
    def checkpoint_db(self) -> Path:
        return self.state_dir / "checkpoints.db"

    @property
    def sessions_db(self) -> Path:
        return self.state_dir / "sessions.db"


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def load_settings(*, require_telegram: bool = True) -> Settings:
    """Build Settings. `require_telegram=False` for the local CLI harness,
    which is fully usable before a bot token exists."""
    load_env_file()

    api_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if not api_key:
        raise ConfigurationError(
            "MOONSHOT_API_KEY is not set (process env or .env)."
        )

    telegram_token = os.environ.get("RC_TELEGRAM_TOKEN", "").strip()
    if require_telegram and not telegram_token:
        raise ConfigurationError(
            "RC_TELEGRAM_TOKEN is not set. Create a new bot with @BotFather "
            "-- do not reuse another bot's token, only one process may poll it."
        )

    chat_raw = os.environ.get("RC_ALLOWED_CHAT_ID", "").strip()
    if require_telegram and not chat_raw:
        raise ConfigurationError("RC_ALLOWED_CHAT_ID is not set.")
    try:
        allowed_chat_id = int(chat_raw) if chat_raw else 0
    except ValueError as exc:
        raise ConfigurationError(
            f"RC_ALLOWED_CHAT_ID must be an integer, got {chat_raw!r}"
        ) from exc

    effort = os.environ.get("RC_REASONING_EFFORT", "high").strip().lower()
    if effort not in {"low", "high", "max"}:
        raise ConfigurationError(
            f"RC_REASONING_EFFORT must be low|high|max, got {effort!r}"
        )

    try:
        execute_timeout = int(os.environ.get("RC_EXECUTE_TIMEOUT", "120"))
    except ValueError as exc:
        raise ConfigurationError("RC_EXECUTE_TIMEOUT must be an integer") from exc

    state_dir = REPO_ROOT / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        api_key=api_key,
        base_url=os.environ.get("RC_BASE_URL", "https://api.moonshot.ai/v1").strip(),
        model=os.environ.get("RC_MODEL", "kimi-k3").strip(),
        small_model=os.environ.get("RC_SMALL_MODEL", "kimi-k2.6").strip(),
        reasoning_effort=effort,
        telegram_token=telegram_token,
        allowed_chat_id=allowed_chat_id,
        interrupt_on=_csv("RC_INTERRUPT_ON", DEFAULT_INTERRUPT_ON),
        execute_timeout=execute_timeout,
        scrub_env=_csv("RC_SCRUB_ENV", ("MOONSHOT_API_KEY", "RC_TELEGRAM_TOKEN")),
        state_dir=state_dir,
    )
