from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class MemberMiddleware(BaseMiddleware):
    """Кладёт в хендлеры `member` — участника, привязанного к этому Telegram-аккаунту (или None)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        data["member"] = await data["db"].get_member_by_tg(user.id) if user else None
        return await handler(event, data)
