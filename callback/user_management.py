from telegram import Update
from telegram.ext import ContextTypes
import logging
from handlers.user_management import (
    users_command,
    start_add_user,
    start_remove_user,
    confirm_remove_user,
    cancel_remove_user
)

logger = logging.getLogger(__name__)

async def handle_user_management_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户管理相关的回调"""
    try:
        callback_data = update.callback_query.data
        
        if callback_data == "add_user":
            await start_add_user(update, context)
        elif callback_data == "remove_user":
            await start_remove_user(update, context)
        elif callback_data == "refresh_users":
            await update.callback_query.answer("🔄 刷新中...")
            await users_command(update, context)
        elif callback_data.startswith("confirm_remove:"):
            await confirm_remove_user(update, context)
        elif callback_data == "cancel_remove":
            await cancel_remove_user(update, context)
        else:
            await update.callback_query.answer("❌ 未知操作")
            logger.warning(f"未知的用户管理回调数据: {callback_data}")
            
    except Exception as e:
        logger.error(f"处理用户管理回调时发生错误: {e}")
        await update.callback_query.answer("❌ 操作失败")
        if update.callback_query.message:
            await update.callback_query.edit_message_text("❌ 操作失败，请重试")