import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramBadRequest,
)
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    source_channel_id: int
    target_chat_id: int
    target_topic_id: int | None
    proxy_url: Optional[str]
    request_timeout: float


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    source = os.getenv("SOURCE_CHANNEL_ID")
    target = os.getenv("TARGET_CHAT_ID")
    topic = os.getenv("TARGET_TOPIC_ID")
    proxy_url = os.getenv("PROXY_URL")
    request_timeout_raw = os.getenv("REQUEST_TIMEOUT", "30")

    if not token or not source or not target:
        raise RuntimeError("Заполните BOT_TOKEN, SOURCE_CHANNEL_ID и TARGET_CHAT_ID в .env")

    try:
        request_timeout = float(request_timeout_raw)
    except ValueError:
        request_timeout = 30.0

    return Settings(
        bot_token=token,
        source_channel_id=int(source),
        target_chat_id=int(target),
        target_topic_id=int(topic) if topic else None,
        proxy_url=proxy_url or None,
        request_timeout=request_timeout,
    )


def build_router(settings: Settings) -> Router:
    router = Router(name="forwarder")

    def parse_message_id(text: str) -> Optional[int]:
        # Ищем последнюю группу цифр в строке (подходит для ссылок t.me/pervyi_shot/123)
        digits = "".join(ch if ch.isdigit() else " " for ch in text).split()
        return int(digits[-1]) if digits else None

    async def forward_message_with_fallback(bot: Bot, *, from_chat_id: int, message_id: int) -> None:
        try:
            await bot.forward_message(
                chat_id=settings.target_chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
                message_thread_id=settings.target_topic_id,
            )
            logger.info(
                "Forwarded message_id=%s from %s to chat %s topic %s",
                message_id,
                from_chat_id,
                settings.target_chat_id,
                settings.target_topic_id or "-",
            )
        except TelegramBadRequest as e:
            if "message thread not found" in str(e) and settings.target_topic_id:
                logger.warning(
                    "Topic %s not found, retrying without topic. message_id=%s",
                    settings.target_topic_id,
                    message_id,
                )
                await bot.forward_message(
                    chat_id=settings.target_chat_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
                logger.info(
                    "Forwarded message_id=%s from %s to chat %s without topic",
                    message_id,
                    from_chat_id,
                    settings.target_chat_id,
                )
            else:
                raise

    @router.message(Command("copy"))
    async def copy_by_id(message: Message, bot: Bot) -> None:
        logger.info(
            "/copy from chat=%s type=%s user=%s text=%s",
            message.chat.id,
            message.chat.type,
            message.from_user.id if message.from_user else "-",
            message.text,
        )
        if not message.text:
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Укажи ссылку на пост или его ID, например:\n/copy https://t.me/pervyi_shot/231")
            return
        msg_id = parse_message_id(parts[1])
        if not msg_id:
            await message.reply("Не нашёл ID сообщения. Формат: /copy https://t.me/pervyi_shot/231")
            return
        try:
            await forward_message_with_fallback(
                bot,
                from_chat_id=settings.source_channel_id,
                message_id=msg_id,
            )
            await message.reply(f"Переслал сообщение {msg_id} из канала.")
        except TelegramForbiddenError as e:
            logger.exception("No rights to forward message_id=%s: %s", msg_id, e)
            await message.reply("Нет прав для пересылки. Проверь, что бот админ в канале и может писать в чат/топик.")
        except TelegramAPIError as e:
            logger.exception("Telegram API error on forward message_id=%s: %s", msg_id, e)
            await message.reply("API Telegram вернул ошибку. См. логи.")
        except Exception:
            logger.exception("Failed to copy message_id=%s", msg_id)
            await message.reply("Не удалось переслать (см. логи).")

    only_source_channel = F.chat.id == settings.source_channel_id

    @router.channel_post(only_source_channel)
    async def forward_channel_post(message: Message, bot: Bot) -> None:
        try:
            await forward_message_with_fallback(
                bot,
                from_chat_id=settings.source_channel_id,
                message_id=message.message_id,
            )
            logger.info(
                "Forwarded post %s from channel %s to chat %s topic %s",
                message.message_id,
                settings.source_channel_id,
                settings.target_chat_id,
                settings.target_topic_id or "-",
            )
        except Exception as e:
            logger.exception(
                "Failed to forward post %s from channel %s: %s",
                message.message_id,
                settings.source_channel_id,
                e,
            )

    @router.edited_channel_post(only_source_channel)
    async def forward_channel_edit(message: Message, bot: Bot) -> None:
        try:
            await forward_message_with_fallback(
                bot,
                from_chat_id=settings.source_channel_id,
                message_id=message.message_id,
            )
            logger.info(
                "Forwarded edited post %s from channel %s to chat %s topic %s",
                message.message_id,
                settings.source_channel_id,
                settings.target_chat_id,
                settings.target_topic_id or "-",
            )
        except Exception as e:
            logger.exception(
                "Failed to forward edited post %s from channel %s: %s",
                message.message_id,
                settings.source_channel_id,
                e,
            )

    @router.channel_post(flags={"block": False})
    async def log_any_channel_post(message: Message) -> None:
        # Диагностический лог: видно, приходят ли события каналов.
        logger.info(
            "channel_post chat_id=%s title=%s msg_id=%s",
            message.chat.id,
            message.chat.title,
            message.message_id,
        )

    @router.message(flags={"block": False})
    async def log_any_message(message: Message) -> None:
        # Диагностика: видно, приходят ли обычные сообщения (например, /copy).
        logger.info(
            "message chat_id=%s type=%s msg_id=%s thread=%s text=%s",
            message.chat.id,
            message.chat.type,
            message.message_id,
            getattr(message, "message_thread_id", None),
            message.text,
        )

    return router


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = load_settings()
    logger.info(
        "Init settings. Source channel: %s -> target chat: %s, topic: %s",
        settings.source_channel_id,
        settings.target_chat_id,
        settings.target_topic_id or "-",
    )
    session = AiohttpSession(
        timeout=settings.request_timeout,
        proxy=settings.proxy_url,
    )
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher()
    dp.include_router(build_router(settings))

    try:
        logger.info("Connecting to Telegram (getMe)...")
        me = await bot.get_me()
        logger.info(
            "Bot %s (@%s) starting. Source channel: %s -> target chat: %s, topic: %s",
            me.id,
            me.username,
            settings.source_channel_id,
            settings.target_chat_id,
            settings.target_topic_id or "-",
        )

        logger.info("Clearing webhook (if any)...")
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook cleared, starting polling...")

        await dp.start_polling(
            bot,
            # Минимально нужные типы: личные сообщения (для /copy) и посты канала.
            allowed_updates=["message", "channel_post", "edited_channel_post"],
        )
    except Exception:
        logger.exception("Bot failed to start")
        raise


if __name__ == "__main__":
    asyncio.run(main())

