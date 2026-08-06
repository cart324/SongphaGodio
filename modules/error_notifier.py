import asyncio
from datetime import datetime
from pathlib import Path
from uuid import uuid4

LOG_RECIPIENT_IDS = (344384179552780289, 395951051980800013)
_LOG_DIRECTORY = Path("error_logs")
_bot = None


def configure_error_notifier(bot) -> None:
    global _bot
    _bot = bot


async def send_error_log(error_log: str, user_name: str | None = None) -> None:
    """Queue an error report for both administrators without raising a second error."""
    if _bot is None:
        return
    if user_name:
        error_log = f"사용자 = {user_name}\n{error_log}"
    await _deliver(error_log)


async def _deliver(error_log: str) -> None:
    if len(error_log) + 7 <= 2000:
        await _send_to_all(message=f"```\n{error_log}```")
        return
    _LOG_DIRECTORY.mkdir(exist_ok=True)
    filename = f"error_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.txt"
    path = _LOG_DIRECTORY / filename
    path.write_text(error_log, encoding="utf-8")
    await _send_to_all(file_path=path, filename=filename)


async def _send_to_all(*, message=None, file_path=None, filename=None) -> None:
    for user_id in LOG_RECIPIENT_IDS:
        try:
            user = await _bot.fetch_user(user_id)
            if file_path is None:
                await user.send(message)
            else:
                import discord
                await user.send(file=discord.File(file_path, filename=filename))
        except Exception:
            pass