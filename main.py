from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from utils import (
    TelegramTransport,
    create_agent_manager,
    load_config,
    restricted,
    setup_logging,
)

logger = setup_logging()
config = load_config()
agent_manager = create_agent_manager(config)
telegram_transport = TelegramTransport(config.allowed_user_ids)


@restricted(config.allowed_user_ids)
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular chat messages using the Ollama LLM Agent"""
    user_message = telegram_transport.get_message_text(update)
    if not user_message:
        return

    user_info = telegram_transport.get_user_info(update)
    logger.info(
        f"Received message from {user_info['username']}: {user_message[:100]}..."
    )

    try:
        await telegram_transport.send_typing_action(update)
        response_text = await agent_manager.process_message(user_message)
        await telegram_transport.send_message(update, response_text)
    except Exception as e:
        logger.error(f"Error getting AI response: {str(e)}", exc_info=True)
        await telegram_transport.send_error_message(
            update, f"getting response from AI:\n{str(e)}"
        )


def main():
    logger.info("Starting Scufris Bot...")

    app = ApplicationBuilder().token(config.telegram_bot_token).build()

    logger.info("Registering message handlers")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    logger.info("Bot is ready! Starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
