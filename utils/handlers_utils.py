# -*- coding: utf-8 -*-
import logging
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)

def wrap_with_session_management(handler_func):
    """包装处理器函数"""
    async def wrapper(update, context):
        return await handler_func(update, context)
    return wrapper


def wrap_conversation_entry_point(handler_func):
    """包装对话入口点处理器，确保在执行新流程前终止当前对话并清理用户数据"""
    async def wrapper(update, context):
        # 记录新命令的开始
        command_name = getattr(handler_func, '__name__', str(handler_func))
        logger.info(f"用户启动新命令: {command_name}")
        
        # 完全清理用户数据，完全模拟cancel的行为
        context.user_data.clear()
        
        # 执行原始处理器函数
        try:
            # 执行原始处理器函数并返回其结果
            # 这确保了新对话能够正确启动并管理状态
            result = await handler_func(update, context)
            return result
        except Exception as e:
            logger.error(f"执行命令时发生错误: {e}")
            # 如果发生错误，仍然确保返回有效的状态
            return ConversationHandler.END
            
    return wrapper