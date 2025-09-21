#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
from typing import Dict, Any
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from utils.permission import check_user_permission, is_admin
from utils.handlers_utils import wrap_conversation_entry_point, wrap_with_session_management
from utils.handlers_fallbacks import get_global_fallbacks
from utils.conversation_states import IDENTIFY_ORIGINAL_NAME, IDENTIFY_ORIGINAL_SEASON, IDENTIFY_TARGET_NAME, IDENTIFY_TARGET_SEASON

logger = logging.getLogger(__name__)

@check_user_permission
async def identify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    /identify 命令入口点
    开始识别词管理流程
    """
    user_id = update.effective_user.id
    
    # 检查管理员权限
    if not is_admin(user_id):
        await update.message.reply_text(
            "❌ 抱歉，只有管理员才能管理识别词配置。",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
    
    await update.message.reply_text(
        "🔧 **识别词管理**\n\n"
        "请按照以下步骤添加新的识别词映射：\n\n"
        "**步骤 1/4**: 请输入需要识别的影视名称\n"
        "例如：中餐厅",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardRemove()
    )
    
    # 初始化用户数据
    context.user_data['identify_data'] = {}
    
    return IDENTIFY_ORIGINAL_NAME

async def identify_original_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    处理原始影视名称输入
    """
    original_name = update.message.text.strip()
    
    if not original_name:
        await update.message.reply_text(
            "❌ 影视名称不能为空，请重新输入："
        )
        return IDENTIFY_ORIGINAL_NAME
    
    # 保存原始名称
    context.user_data['identify_data']['original_name'] = original_name
    
    await update.message.reply_text(
        f"✅ 原始影视名称：**{original_name}**\n\n"
        f"**步骤 2/4**: 请输入需要识别的影视季度(电影写0即可)\n"
        f"例如：9",
        parse_mode='Markdown'
    )
    
    return IDENTIFY_ORIGINAL_SEASON

async def identify_original_season_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    处理原始季度输入
    """
    season_text = update.message.text.strip()
    
    try:
        original_season = int(season_text)
        if original_season < 0:
            raise ValueError("季度必须是非负整数")
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入有效的季度数字"
        )
        return IDENTIFY_ORIGINAL_SEASON
    
    # 保存原始季度
    context.user_data['identify_data']['original_season'] = original_season
    
    # 根据季度显示不同的确认信息
    if original_season == 0:
        season_display = "**电影**"
    else:
        season_display = f"**第{original_season}季**"
    
    await update.message.reply_text(
        f"✅ 原始季度：{season_display}\n\n"
        f"**步骤 3/4**: 请输入识别后的影视名称\n"
        f"例如：中餐厅·非洲创业季",
        parse_mode='Markdown'
    )
    
    return IDENTIFY_TARGET_NAME

async def identify_target_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    处理目标影视名称输入
    """
    target_name = update.message.text.strip()
    
    if not target_name:
        await update.message.reply_text(
            "❌ 目标影视名称不能为空，请重新输入："
        )
        return IDENTIFY_TARGET_NAME
    
    # 保存目标名称
    context.user_data['identify_data']['target_name'] = target_name
    
    await update.message.reply_text(
        f"✅ 目标影视名称：**{target_name}**\n\n"
        f"**步骤 4/4**: 请输入识别后的影视季度（电影写0即可）\n"
        f"例如：1",
        parse_mode='Markdown'
    )
    
    return IDENTIFY_TARGET_SEASON

async def identify_target_season_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    处理目标季度输入并完成识别词添加
    """
    season_text = update.message.text.strip()
    
    try:
        target_season = int(season_text)
        if target_season < 0:
            raise ValueError("季度必须是非负整数")
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入有效的季度数字"
        )
        return IDENTIFY_TARGET_SEASON
    
    # 保存目标季度
    context.user_data['identify_data']['target_season'] = target_season
    
    # 获取完整的识别数据
    identify_data = context.user_data['identify_data']
    original_name = identify_data['original_name']
    original_season = identify_data['original_season']
    target_name = identify_data['target_name']
    target_season = identify_data['target_season']
    
    # 构建识别词映射规则
    # 如果季度为0（电影），则不包含S00信息
    if original_season == 0:
        original_part = original_name
    else:
        original_part = f"{original_name} S{original_season:02d}"
    
    if target_season == 0:
        target_part = target_name
    else:
        target_part = f"{target_name} S{target_season:02d}"
    
    mapping_rule = f"{original_part} => {target_part}"
    
    # 显示确认信息
    # 根据季度显示不同的确认信息
    if original_season == 0:
        original_display = f"{original_name} (电影)"
    else:
        original_display = f"{original_name} 第{original_season}季"
    
    if target_season == 0:
        target_display = f"{target_name} (电影)"
    else:
        target_display = f"{target_name} 第{target_season}季"
    
    await update.message.reply_text(
        f"📋 **识别词映射确认**\n\n"
        f"**原始**: {original_display}\n"
        f"**目标**: {target_display}\n\n"
        f"**映射规则**: `{mapping_rule}`\n\n"
        f"正在添加到配置文件...",
        parse_mode='Markdown'
    )
    
    # 添加到identify.txt文件
    success = await add_identify_mapping(mapping_rule)
    
    if success:
        await update.message.reply_text(
            f"✅ **识别词添加成功！**\n\n"
            f"映射规则已添加到配置文件：\n"
            f"`{mapping_rule}`\n\n"
            f"现在Emby webhook会自动将 **{original_part}** 转换为 **{target_part}**",
            parse_mode='Markdown'
        )
        logger.info(f"✅ 用户 {update.effective_user.id} 添加识别词映射: {mapping_rule}")
    else:
        await update.message.reply_text(
            f"❌ **识别词添加失败！**\n\n"
            f"无法写入配置文件，请检查文件权限或联系管理员。",
            parse_mode='Markdown'
        )
        logger.error(f"❌ 用户 {update.effective_user.id} 添加识别词映射失败: {mapping_rule}")
    
    # 清理用户数据
    context.user_data.pop('identify_data', None)
    
    return ConversationHandler.END

async def identify_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    取消识别词管理流程
    """
    await update.message.reply_text(
        "❌ 识别词管理已取消。",
        reply_markup=ReplyKeyboardRemove()
    )
    
    # 清理用户数据
    context.user_data.pop('identify_data', None)
    
    return ConversationHandler.END

async def add_identify_mapping(mapping_rule: str) -> bool:
    """
    将识别词映射规则添加到identify.txt文件
    
    Args:
        mapping_rule: 映射规则字符串，格式如 "中餐厅 S09 => 中餐厅·非洲创业季 S01"
        
    Returns:
        bool: 添加成功返回True，失败返回False
    """
    try:
        from utils.identify_config import get_identify_config_path, initialize_identify_config
        
        config_file = get_identify_config_path()
        
        # 确保配置文件存在
        if not initialize_identify_config():
            logger.error("❌ 无法初始化识别词配置文件")
            return False
        
        # 检查规则是否已存在
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                existing_content = f.read()
                if mapping_rule in existing_content:
                    logger.warning(f"⚠️ 识别词映射规则已存在: {mapping_rule}")
                    return True  # 已存在也算成功
        
        # 追加新规则到文件
        with open(config_file, 'a', encoding='utf-8') as f:
            f.write(f"{mapping_rule}\n")
        
        logger.info(f"✅ 识别词映射规则已添加: {mapping_rule}")
        return True
        
    except Exception as e:
        logger.error(f"❌ 添加识别词映射规则失败: {e}")
        return False


def create_identify_handler():
    """创建识别词管理对话处理器"""
    return ConversationHandler(
        entry_points=[CommandHandler("identify", wrap_conversation_entry_point(identify_command))],
        states={
            IDENTIFY_ORIGINAL_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    wrap_with_session_management(identify_original_name_input)
                )
            ],
            IDENTIFY_ORIGINAL_SEASON: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    wrap_with_session_management(identify_original_season_input)
                )
            ],
            IDENTIFY_TARGET_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    wrap_with_session_management(identify_target_name_input)
                )
            ],
            IDENTIFY_TARGET_SEASON: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    wrap_with_session_management(identify_target_season_input)
                )
            ],
        },
        fallbacks=get_global_fallbacks(),
        per_chat=True,
        per_user=True,
        allow_reentry=True,
        persistent=False
    )