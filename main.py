from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from utils import (
    TelegramTransport,
    ToolCallbackHandler,
    create_agent_manager,
    create_history_manager,
    load_config,
    restricted,
    setup_logging,
    setup_scufris,
    truncate_log,
)

logger = setup_logging()
config = load_config()
telegram_transport = TelegramTransport(config.allowed_user_ids)
history_manager = create_history_manager(config.max_history_per_user)

# Setup the agent hierarchy
main_agent = setup_scufris(config=config)

# Create callback handler and agent manager
callback_handler = ToolCallbackHandler(telegram_transport)
callbacks = [callback_handler]

agent_manager = create_agent_manager(
    agent=main_agent,
    callbacks=callbacks,
)


@restricted(config.allowed_user_ids)
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular chat messages using the Ollama LLM Agent"""
    import time

    request_start = time.time()

    user_message = telegram_transport.get_message_text(update)
    if not user_message:
        return

    user_info = telegram_transport.get_user_info(update)
    user_id = user_info["id"]

    logger.info(
        f"User {user_info['username']} (ID:{user_id}): {truncate_log(user_message, 100)}"
    )

    try:
        # Set update in callback handler for status updates
        callback_handler.set_update(update)

        # Send typing action
        await telegram_transport.send_typing_action(update)

        # Get history with new message for the agent
        messages = history_manager.get_history_with_new_message(user_id, user_message)

        logger.debug(f"Processing {len(messages)} messages in history")

        # Process the message with history
        process_start = time.time()
        response_text = await agent_manager.process_message(messages)
        process_duration = time.time() - process_start

        # Add messages to history
        history_manager.add_user_message(user_id, user_message)
        history_manager.add_ai_message(user_id, response_text)

        # Send the response
        send_start = time.time()
        await telegram_transport.send_message(update, response_text)
        send_duration = time.time() - send_start

        total_duration = time.time() - request_start

        logger.info(
            f"Request completed | total={total_duration:.2f}s "
            f"(process={process_duration:.2f}s, send={send_duration:.2f}s) | "
            f"response={len(response_text)} chars"
        )

    except Exception as e:
        logger.error(
            f"Error processing request for user {user_id}: {str(e)}", exc_info=True
        )
        await telegram_transport.send_error_message(
            update, f"getting response from AI:\n{str(e)}"
        )
    finally:
        # Clear the update from callback handler after processing
        callback_handler.set_update(None)


@restricted(config.allowed_user_ids)
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear chat history for the user"""
    user_info = telegram_transport.get_user_info(update)
    user_id = user_info["id"]

    message_count = history_manager.get_message_count(user_id)
    history_manager.clear_history(user_id)

    logger.info(
        f"Cleared {message_count} messages for user {user_info['username']} (ID:{user_id})"
    )

    await update.message.reply_text(
        f"🗑️ Cleared {message_count} messages from your chat history."
    )


@restricted(config.allowed_user_ids)
async def history_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show chat history statistics"""
    user_info = telegram_transport.get_user_info(update)
    user_id = user_info["id"]

    message_count = history_manager.get_message_count(user_id)
    stats = history_manager.get_stats()

    stats_text = (
        f"📊 Chat History Stats\n\n"
        f"Your messages: {message_count}\n"
        f"Max per user: {stats['max_history_per_user']}\n"
        f"Total users: {stats['total_users']}\n"
        f"Total messages: {stats['total_messages']}"
    )

    await update.message.reply_text(stats_text)


def main():
    logger.info("Starting Scufris Bot...")

    app = ApplicationBuilder().token(config.telegram_bot_token).build()

    logger.info("Registering command handlers")
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("history", history_stats))

    logger.info("Registering message handlers")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    logger.info("Bot is ready! Starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
