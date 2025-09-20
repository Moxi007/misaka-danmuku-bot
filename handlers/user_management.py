from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import logging
from config import ConfigManager
from utils.permission import check_admin_permission

logger = logging.getLogger(__name__)

# 定义状态
USER_ID_INPUT = 1
CONFIRM_ACTION = 2

async def show_users_list_as_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """发送新消息显示用户列表（用于添加用户后）"""
    try:
        config_manager = ConfigManager()
        allowed_users = config_manager.get_allowed_users()
        admin_users = config_manager.get_admin_users()
        
        # 构建消息文本
        message_lines = ["👥 **用户权限管理**\n"]
        
        # 显示管理员列表
        message_lines.append("🔑 **超级管理员** (不可删除):")
        if admin_users:
            for admin_id in admin_users:
                message_lines.append(f"   • `{admin_id}`")
        else:
            message_lines.append("   暂无管理员")
        
        message_lines.append("")
        
        # 显示普通用户列表
        regular_users = [uid for uid in allowed_users if uid not in admin_users]
        message_lines.append("👤 **普通用户**:")
        if regular_users:
            for user_id in regular_users:
                message_lines.append(f"   • `{user_id}`")
        else:
            message_lines.append("   暂无普通用户")
        
        # 构建键盘
        keyboard = [
            [InlineKeyboardButton("➕ 添加用户", callback_data="add_user")],
        ]
        
        # 如果有普通用户，添加删除按钮
        if regular_users:
            keyboard.append([InlineKeyboardButton("🗑️ 删除用户", callback_data="remove_user")])
        
        keyboard.append([InlineKeyboardButton("🔄 刷新列表", callback_data="refresh_users")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = "\n".join(message_lines)
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
    except Exception as e:
        logger.error(f"发送用户列表消息时发生错误: {e}")
        await update.message.reply_text("❌ 获取用户列表失败")

@check_admin_permission
async def show_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示用户管理界面"""
    try:
        config_manager = ConfigManager()
        allowed_users = config_manager.get_allowed_users()
        admin_users = config_manager.get_admin_users()
        
        # 构建消息文本
        message_lines = ["👥 **用户权限管理**\n"]
        
        # 显示管理员列表
        message_lines.append("🔑 **超级管理员** (不可删除):")
        if admin_users:
            for admin_id in admin_users:
                message_lines.append(f"   • `{admin_id}`")
        else:
            message_lines.append("   暂无管理员")
        
        message_lines.append("")
        
        # 显示普通用户列表
        regular_users = [uid for uid in allowed_users if uid not in admin_users]
        message_lines.append("👤 **普通用户**:")
        if regular_users:
            for user_id in regular_users:
                message_lines.append(f"   • `{user_id}`")
        else:
            message_lines.append("   暂无普通用户")
        
        # 构建键盘
        keyboard = [
            [InlineKeyboardButton("➕ 添加用户", callback_data="add_user")],
        ]
        
        # 如果有普通用户，添加删除按钮
        if regular_users:
            keyboard.append([InlineKeyboardButton("🗑️ 删除用户", callback_data="remove_user")])
        
        keyboard.append([InlineKeyboardButton("🔄 刷新列表", callback_data="refresh_users")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = "\n".join(message_lines)
        
        if update.message:
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            # 检查消息内容是否相同，避免Telegram API错误
            try:
                await update.callback_query.edit_message_text(
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            except Exception as edit_error:
                # 如果编辑失败（通常是因为内容相同），只回答callback query
                if "not modified" in str(edit_error).lower():
                    await update.callback_query.answer("✅ 列表已是最新状态")
                else:
                    # 其他错误重新抛出
                    raise edit_error
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"显示用户列表时发生错误: {e}")
        error_msg = "❌ 获取用户列表失败"
        if update.message:
            await update.message.reply_text(error_msg)
        else:
            await update.callback_query.edit_message_text(error_msg)
        return ConversationHandler.END

@check_admin_permission
async def start_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始添加用户流程"""
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "➕ **添加用户**\n\n请输入要添加的用户ID:\n\n💡 提示: 用户ID是纯数字，可以通过转发用户消息给 @userinfobot 获取",
        parse_mode='Markdown'
    )
    context.user_data['action'] = 'add'
    return USER_ID_INPUT

@check_admin_permission
async def start_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始删除用户流程"""
    await update.callback_query.answer()
    
    config_manager = ConfigManager()
    allowed_users = config_manager.get_allowed_users()
    admin_users = config_manager.get_admin_users()
    regular_users = [uid for uid in allowed_users if uid not in admin_users]
    
    if not regular_users:
        await update.callback_query.edit_message_text(
            "❌ 暂无可删除的普通用户\n\n💡 提示: 超级管理员不能被删除"
        )
        return ConversationHandler.END
    
    # 构建用户选择键盘
    keyboard = []
    for user_id in regular_users:
        keyboard.append([InlineKeyboardButton(f"🗑️ 删除 {user_id}", callback_data=f"confirm_remove:{user_id}")])
    
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel_remove")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        "🗑️ **删除用户**\n\n请选择要删除的用户:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    return CONFIRM_ACTION

@check_admin_permission
async def handle_user_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户ID输入"""
    user_input = update.message.text.strip()
    action = context.user_data.get('action')
    
    # 验证输入是否为有效的用户ID
    if not user_input.isdigit():
        await update.message.reply_text(
            "❌ 无效的用户ID，请输入纯数字\n\n请重新输入用户ID:"
        )
        return USER_ID_INPUT
    
    user_id = int(user_input)
    
    if user_id <= 0:
        await update.message.reply_text(
            "❌ 用户ID必须大于0\n\n请重新输入用户ID:"
        )
        return USER_ID_INPUT
    
    config_manager = ConfigManager()
    
    if action == 'add':
        # 添加用户
        if config_manager.is_user_allowed(user_id):
            await update.message.reply_text(
                f"ℹ️ 用户 `{user_id}` 已在允许列表中",
                parse_mode='Markdown'
            )
        else:
            success = config_manager.add_allowed_user(user_id)
            if success:
                # 成功添加用户后，直接显示更新后的用户列表
                await show_users_list_as_new_message(update, context)
            else:
                await update.message.reply_text(
                    f"❌ 添加用户 `{user_id}` 失败",
                    parse_mode='Markdown'
                )
    
    # 清理用户数据
    context.user_data.clear()
    return ConversationHandler.END

@check_admin_permission
async def confirm_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """确认删除用户"""
    await update.callback_query.answer()
    
    callback_data = update.callback_query.data
    user_id = int(callback_data.split(":")[1])
    
    config_manager = ConfigManager()
    
    # 检查用户是否为管理员
    if config_manager.is_user_admin(user_id):
        await update.callback_query.edit_message_text(
            f"❌ 不能删除管理员用户 `{user_id}`",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # 删除用户
    success = config_manager.remove_allowed_user(user_id)
    if success:
        # 成功删除用户后，直接显示更新后的用户列表
        await show_users_list(update, context)
    else:
        await update.callback_query.edit_message_text(
            f"❌ 移除用户 `{user_id}` 失败",
            parse_mode='Markdown'
        )
    
    return ConversationHandler.END

@check_admin_permission
async def cancel_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消删除用户"""
    await update.callback_query.answer()
    # 取消删除操作后，直接显示用户列表
    await show_users_list(update, context)
    return ConversationHandler.END

@check_admin_permission
async def cancel_user_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消用户管理操作"""
    context.user_data.clear()
    # 取消操作后直接显示用户列表，而不是仅显示取消信息
    await show_users_list_as_new_message(update, context)
    return ConversationHandler.END


def create_user_management_handler():
    """创建用户管理ConversationHandler"""
    return ConversationHandler(
        entry_points=[
            CommandHandler("users", show_users_list),
            CallbackQueryHandler(start_add_user, pattern="^add_user$"),
            CallbackQueryHandler(start_remove_user, pattern="^remove_user$"),
            CallbackQueryHandler(lambda u, c: show_users_list(u, c), pattern="^refresh_users$")
        ],
        states={
            USER_ID_INPUT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_user_id_input
                ),
                CommandHandler("users", show_users_list)
            ],
            CONFIRM_ACTION: [
                CallbackQueryHandler(confirm_remove_user, pattern="^confirm_remove:.*$"),
                CallbackQueryHandler(cancel_remove_user, pattern="^cancel_remove$"),
                CommandHandler("users", show_users_list)
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_user_management)
        ],
        per_chat=True,
        per_user=True
    )