from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler
import logging
from utils.api import call_danmaku_api
from utils.permission import check_user_permission
from utils.conversation_states import TOKEN_NAME_INPUT, VALIDITY_PERIOD_SELECT

logger = logging.getLogger(__name__)

# 有效期选项
VALIDITY_PERIODS = [
    {'value': 'permanent', 'label': '永久'},
    {'value': '1d', 'label': '1 天'},
    {'value': '7d', 'label': '7 天'},
    {'value': '30d', 'label': '30 天'},
    {'value': '180d', 'label': '6 个月'},
    {'value': '365d', 'label': '1 年'},
]

@check_user_permission
async def handle_token_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理token管理相关的callback查询"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    
    if callback_data == "add_token":
        return await start_add_token(update, context)
    elif callback_data.startswith("toggle_token:"):
        token_id = callback_data.split(":")[1]
        await toggle_token_status(update, context, token_id)
        return ConversationHandler.END
    elif callback_data.startswith("delete_token:"):
        token_id = callback_data.split(":")[1]
        await confirm_delete_token(update, context, token_id)
        return ConversationHandler.END
    elif callback_data.startswith("confirm_delete:"):
        token_id = callback_data.split(":")[1]
        await delete_token(update, context, token_id)
        return ConversationHandler.END
    elif callback_data == "cancel_delete":
        await cancel_delete(update, context)
        return ConversationHandler.END
    elif callback_data.startswith("validity:"):
        validity_period = callback_data.split(":")[1]
        return await create_token_with_validity(update, context, validity_period)
    
    return ConversationHandler.END

async def start_add_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始添加token流程"""
    await update.callback_query.edit_message_text(
        "📝 **添加新Token**\n\n请输入Token名称:",
        parse_mode='Markdown'
    )
    return TOKEN_NAME_INPUT

async def create_token_with_validity(update: Update, context: ContextTypes.DEFAULT_TYPE, validity_period: str):
    """使用指定有效期创建token"""
    try:
        token_name = context.user_data.get('token_name')
        if not token_name:
            await update.callback_query.edit_message_text("❌ Token名称丢失，请重新开始")
            return ConversationHandler.END
        
        # 调用API创建token
        payload = {
            'name': token_name,
            'validityPeriod': validity_period
        }
        
        response = call_danmaku_api(
            'POST',
            '/tokens',
            json_data=payload
        )
        
        if response and response.get('success'):
            token_data = response.get('data', {})
            token_value = token_data.get('token', 'N/A')
            
            # 获取有效期标签
            validity_label = next(
                (period['label'] for period in VALIDITY_PERIODS if period['value'] == validity_period),
                validity_period
            )
            
            await update.callback_query.edit_message_text(
                f"✅ **Token创建成功!**\n\n"
                f"📝 **名称:** {token_name}\n"
                f"🔑 **Token:** `{token_value}`\n"
                f"⏰ **有效期:** {validity_label}\n\n",
                parse_mode='Markdown'
            )
        else:
            error_msg = response.get('message', '未知错误') if response else 'API调用失败'
            await update.callback_query.edit_message_text(f"❌ 创建Token失败: {error_msg}")
        
        # 清理用户数据
        context.user_data.pop('token_name', None)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"创建token时发生错误: {e}")
        await update.callback_query.edit_message_text("❌ 创建Token时发生错误")
        return ConversationHandler.END

async def refresh_tokens_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """刷新tokens列表"""
    try:
        # 调用API获取tokens列表
        response = call_danmaku_api('GET', '/tokens')
        
        if not response or not response.get('success'):
            await update.callback_query.edit_message_text("❌ 获取tokens列表失败")
            return ConversationHandler.END
        
        tokens = response.get('data', [])
        
        if not tokens:
            # 没有tokens时显示添加按钮
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = [[InlineKeyboardButton("➕ 添加Token", callback_data="add_token")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.callback_query.edit_message_text(
                "🔑 **Token管理**\n\n📝 暂无Token，点击下方按钮添加新Token。",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return ConversationHandler.END
        
        # 构建tokens列表消息
        message_lines = ["🔑 **Token管理**\n"]
        
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = []
        
        for i, token in enumerate(tokens, 1):
            token_id = token.get('id')
            name = token.get('name', 'N/A')
            is_enabled = token.get('isEnabled', False)
            expires_at = token.get('expiresAt', 'N/A')
            created_at = token.get('createdAt', 'N/A')
            
            # 状态显示
            status = "🟢 启用" if is_enabled else "🔴 禁用"
            
            message_lines.append(
                f"**{i}. {name}**\n"
                f"   状态: {status}\n"
                f"   过期时间: {expires_at}\n"
                f"   创建时间: {created_at}\n"
            )
            
            # 为每个token添加操作按钮
            button_text = "禁用" if is_enabled else "启用"
            keyboard.append([
                InlineKeyboardButton(f"{button_text} {name}", callback_data=f"toggle_token:{token_id}"),
                InlineKeyboardButton(f"🗑️ 删除 {name}", callback_data=f"delete_token:{token_id}")
            ])
        
        # 添加操作按钮
        keyboard.append([InlineKeyboardButton("➕ 添加Token", callback_data="add_token")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = "\n".join(message_lines)
        
        await update.callback_query.edit_message_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"刷新tokens列表时发生错误: {e}")
        await update.callback_query.edit_message_text("❌ 刷新tokens列表时发生错误")
        return ConversationHandler.END

async def toggle_token_status(update: Update, context: ContextTypes.DEFAULT_TYPE, token_id: str):
    """切换token状态"""
    try:
        # 调用API切换token状态
        response = call_danmaku_api(
            'PUT',
            f'/tokens/{token_id}/toggle'
        )
        
        if response and response.get('success'):
            await update.callback_query.answer("✅ Token状态已更新")
            # 刷新列表
            return await refresh_tokens_list(update, context)
        else:
            error_msg = response.get('message', '未知错误') if response else 'API调用失败'
            await update.callback_query.answer(f"❌ 更新失败: {error_msg}")
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"切换token状态时发生错误: {e}")
        await update.callback_query.answer("❌ 操作失败")
        return ConversationHandler.END

async def confirm_delete_token(update: Update, context: ContextTypes.DEFAULT_TYPE, token_id: str):
    """确认删除token"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    keyboard = [
        [InlineKeyboardButton("✅ 确认删除", callback_data=f"confirm_delete:{token_id}")],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel_delete")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        "⚠️ **确认删除Token**\n\n确定要删除这个Token吗？此操作不可撤销。",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def delete_token(update: Update, context: ContextTypes.DEFAULT_TYPE, token_id: str):
    """删除token"""
    try:
        # 调用API删除token
        response = call_danmaku_api(
            'DELETE',
            f'/tokens/{token_id}'
        )
        
        if response and response.get('success'):
            await update.callback_query.answer("✅ Token已删除")
            # 刷新列表
            return await refresh_tokens_list(update, context)
        else:
            error_msg = response.get('message', '未知错误') if response else 'API调用失败'
            await update.callback_query.answer(f"❌ 删除失败: {error_msg}")
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"删除token时发生错误: {e}")
        await update.callback_query.answer("❌ 删除失败")
        return ConversationHandler.END

async def cancel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消删除操作"""
    return await refresh_tokens_list(update, context)