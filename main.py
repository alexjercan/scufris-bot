import logging
from datetime import datetime, timezone

from langchain_core.callbacks import BaseCallbackHandler
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
    create_compactor,
    create_history_manager,
    load_config,
    restricted,
    setup_logging,
    setup_scufris,
    truncate_log,
)
from utils.stats import format_stats_lines
from utils.telemetry import begin_turn

logger = setup_logging(default_level=logging.INFO)
config = load_config()
telegram_transport = TelegramTransport(config.allowed_user_ids)
history_manager = create_history_manager(
    config.max_history_per_user, compactor=create_compactor()
)

# Captured once at process start so /stats can show uptime.
session_started_at = datetime.now(timezone.utc)

# Setup the agent hierarchy
main_agent = setup_scufris(config=config, history_manager=history_manager)

# Create callback handler and agent manager
callback_handler = ToolCallbackHandler(telegram_transport)
callbacks: list[BaseCallbackHandler] = [callback_handler]

agent_manager = create_agent_manager(
    agent=main_agent,
    callbacks=callbacks,
    history_manager=history_manager,
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
        with begin_turn(f"telegram:{user_id}"):
            response_text = await agent_manager.process_message(messages, user_id)
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

    breakdown = history_manager.get_user_breakdown(user_id)
    total = history_manager.clear_history(user_id)

    logger.info(
        f"Cleared {total} messages for user {user_info['username']} (ID:{user_id})"
    )

    if total == 0:
        msg = "🗑️ No messages to clear."
    elif breakdown:
        parts = ", ".join(f"{a}: {n}" for a, n in sorted(breakdown.items()))
        msg = f"🗑️ Cleared {total} messages ({parts})."
    else:
        msg = f"🗑️ Cleared {total} messages from your chat history."

    assert update.message is not None, "Update has no message"
    await update.message.reply_text(msg)


@restricted(config.allowed_user_ids)
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show per-agent memory + telemetry breakdown for the user."""
    user_info = telegram_transport.get_user_info(update)
    user_id = user_info["id"]

    lines = format_stats_lines(
        history_manager,
        user_id,
        started_at=session_started_at,
        model=config.ollama_model,
        base_url=config.ollama_base_url,
    )
    body = "\n".join(lines)
    # Wrap in monospace for stable column alignment on Telegram.
    assert update.message is not None, "Update has no message"
    await update.message.reply_text(f"```\n{body}\n```", parse_mode="Markdown")


def main():
    logger.info("Starting Scufris Bot...")

    app = ApplicationBuilder().token(config.telegram_bot_token).build()

    logger.info("Registering command handlers")
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("stats", stats_command))

    logger.info("Registering message handlers")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    logger.info("Bot is ready! Starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
