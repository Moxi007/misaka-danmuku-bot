# -*- coding: utf-8 -*-

import logging
from typing import Dict, Any
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from utils.permission import check_user_permission, is_admin
from utils.blacklist_config import add_blacklist_item, load_blacklist, get_blacklist_stats

logger = logging.getLogger(__name__)

# 对话状态常量
BLACKLIST_NAME_INPUT = 0

@check_user_permission
async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /blacklist 命令入口点
    开始黑名单管理流程
    """
    user_id = update.effective_user.id
    
    # 检查管理员权限
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ 抱歉，只有管理员才能管理黑名单配置。",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    
    # 获取当前黑名单统计信息
    stats = get_blacklist_stats()
    blacklist_count = stats.get('blacklist_count', 0)
    
    await update.message.reply_text(
        f"🔧 **黑名单管理**\n\n"
        f"当前黑名单中有 **{blacklist_count}** 个影视名称。\n\n"
        f"请输入要添加到黑名单的影视名称。添加后，包含该名称的影视将不会被自动导入或刷新。\n",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardRemove()
    )
    
    return BLACKLIST_NAME_INPUT

async def blacklist_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    处理黑名单影视名称输入
    """
    media_name = update.message.text.strip()
    
    if not media_name:
        await update.message.reply_text(
            "❌ 影视名称不能为空，请重新输入："
        )
        return BLACKLIST_NAME_INPUT
    
    # 添加到黑名单
    success = add_blacklist_item(media_name)
    
    if success:
        await update.message.reply_text(
            f"✅ **黑名单添加成功！**\n\n"
            f"影视名称 **{media_name}** 已添加到黑名单。\n\n"
            f"现在Emby webhook会自动阻止包含此名称的影视入库和刷新。",
            parse_mode='Markdown'
        )
        logger.info(f"✅ 用户 {update.effective_user.id} 添加黑名单影视名称: {media_name}")
    else:
        await update.message.reply_text(
            f"❌ **黑名单添加失败！**\n\n"
            f"无法写入配置文件，请检查文件权限或联系管理员。",
            parse_mode='Markdown'
        )
        logger.error(f"❌ 用户 {update.effective_user.id} 添加黑名单影视名称失败: {media_name}")
    
    # 提供查看当前黑名单或继续添加的选项
    await update.message.reply_text(
        "🔧 **黑名单管理**\n\n"
        "你可以：\n"
        "• 输入新的影视名称继续添加到黑名单\n"
        "• 使用 /cancel 取消当前操作\n"
        "• 使用 /blacklist 查看当前黑名单状态",
        parse_mode='Markdown'
    )
    
    return BLACKLIST_NAME_INPUT

async def blacklist_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    取消黑名单管理流程
    """
    await update.message.reply_text(
        "❌ 黑名单管理已取消。",
        reply_markup=ReplyKeyboardRemove()
    )
    
    return ConversationHandler.END

def create_blacklist_handler():    
    """
    创建黑名单管理ConversationHandler
    """
    # 避免循环导入，在函数内部导入
    from bot import _wrap_conversation_entry_point
    from bot import _wrap_with_session_management
    
    return ConversationHandler(
        entry_points=[CommandHandler("blacklist", _wrap_conversation_entry_point(blacklist_command))],
        states={
            BLACKLIST_NAME_INPUT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    _wrap_with_session_management(blacklist_name_input)
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _wrap_with_session_management(blacklist_cancel)),
            CommandHandler("start", _wrap_with_session_management(blacklist_cancel)),
            CommandHandler("help", _wrap_with_session_management(blacklist_cancel)),
            CommandHandler("search", _wrap_with_session_management(blacklist_cancel)),
            CommandHandler("auto", _wrap_with_session_management(blacklist_cancel))
        ],
        per_chat=True,
        per_user=True,
    )
