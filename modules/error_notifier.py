import asyncio


LOG_RECIPIENT_IDS = (344384179552780289, 395951051980800013)


class ErrorLogRecipients:
    """Send one error report to every configured administrator via DM."""

    def __init__(self, bot):
        self.bot = bot

    async def send(self, message: str):
        # Discord DMs are limited to 2,000 characters.
        if len(message) > 2000:
            message = message[:1997] + "..."

        results = await asyncio.gather(
            *(self._send_to(user_id, message) for user_id in LOG_RECIPIENT_IDS),
            return_exceptions=True,
        )
        for user_id, result in zip(LOG_RECIPIENT_IDS, results):
            if isinstance(result, Exception):
                print(f"[WARNING] Failed to send error log to {user_id}: {result}")

    async def _send_to(self, user_id: int, message: str):
        user = await self.bot.fetch_user(user_id)
        await user.send(message)


async def fetch_log_recipients(bot) -> ErrorLogRecipients:
    """Return a DM broadcaster that resolves recipients with fetch_user()."""
    return ErrorLogRecipients(bot)