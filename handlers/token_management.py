from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import logging
from utils.api import call_danmaku_api
from utils.permission import check_admin_permission
from utils.conversation_states import TOKEN_NAME_INPUT, VALIDITY_PERIOD_SELECT
from utils.handlers_utils import wrap_conversation_entry_point
from utils.handlers_fallbacks import get_global_fallbacks

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

@check_admin_permission
async def tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示tokens列表"""
    try:
        # 调用API获取tokens列表
        response = call_danmaku_api('GET', '/tokens')
        
        if not response or 'success' not in response:
            await update.message.reply_text("❌ 获取tokens列表失败")
            return ConversationHandler.END
        
        if not response['success']:
            error_msg = response.get('message', '未知错误')
            await update.message.reply_text(f"❌ 获取tokens列表失败: {error_msg}")
            return ConversationHandler.END
        
        tokens = response.get('data', [])
        
        # 构建消息文本和inline键盘
        keyboard = []
        
        if not tokens:
            message_text = "📋 **Token 管理**\n\n暂无tokens"
        else:
            message_text = "📋 **Token 管理**\n\n"
            for i, token in enumerate(tokens, 1):
                token_id = token.get('id', 'N/A')
                name = token.get('name', 'N/A')
                status = "🟢 启用" if token.get('isEnabled', False) else "🔴 禁用"
                expires_at = token.get('expiresAt', 'N/A')
                created_at = token.get('createdAt', 'N/A')
                enabled = token.get('isEnabled', False)
                
                message_text += f"{i}. **{name}**\n"
                message_text += f"   ID: `{token_id}`\n"
                message_text += f"   状态: {status}\n"
                message_text += f"   过期时间: {expires_at}\n"
                message_text += f"   创建时间: {created_at}\n\n"
                
                # 为每个token添加操作按钮（紧跟在token信息后面）
                toggle_text = "🔴 禁用" if enabled else "🟢 启用"
                toggle_callback = f"toggle_token:{token_id}"
                delete_callback = f"delete_token:{token_id}"
                
                keyboard.append([
                    InlineKeyboardButton(f"{toggle_text} {name}", callback_data=toggle_callback),
                    InlineKeyboardButton("🗑️ 删除", callback_data=delete_callback)
                ])
        
        # 添加通用操作按钮
        keyboard.append([InlineKeyboardButton("➕ 添加Token", callback_data="add_token")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        return ConversationHandler.END
        
    except Exception as e:
        logger.error(f"显示tokens列表时发生错误: {e}")
        await update.message.reply_text("❌ 获取tokens列表时发生错误")
        return ConversationHandler.END

# Callback相关函数已移动到 callback/token_management.py

async def handle_token_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理token名称输入"""
    token_name = update.message.text.strip()
    
    if not token_name:
        await update.message.reply_text("❌ Token名称不能为空，请重新输入:")
        return TOKEN_NAME_INPUT
    
    # 保存token名称到context
    context.user_data['token_name'] = token_name
    
    # 显示有效期选择
    keyboard = []
    for period in VALIDITY_PERIODS:
        keyboard.append([InlineKeyboardButton(
            period['label'], 
            callback_data=f"validity:{period['value']}"
        )])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"📅 **选择有效期**\n\nToken名称: `{token_name}`\n\n请选择Token的有效期:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    return VALIDITY_PERIOD_SELECT

# create_token_with_validity 函数已移动到 callback/token_management.py

# refresh_tokens_list 函数已移动到 callback/token_management.py

# toggle_token_status 函数已移动到 callback/token_management.py

# confirm_delete_token 函数已移动到 callback/token_management.py

# delete_token 函数已移动到 callback/token_management.py

# cancel_delete 函数已移动到 callback/token_management.py

async def cancel_token_operation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消token操作"""
    # 清理用户数据
    context.user_data.pop('token_name', None)
    
    await update.message.reply_text("❌ 操作已取消")
    return ConversationHandler.END

def create_token_management_handler():
    """创建token管理命令处理器"""
    from callback.token_management import handle_token_callback_query
    
    return ConversationHandler(
        entry_points=[
            CommandHandler('tokens', wrap_conversation_entry_point(tokens_command)),
            CallbackQueryHandler(handle_token_callback_query, pattern=r'^add_token$'),
            CallbackQueryHandler(handle_token_callback_query, pattern=r'^(toggle_token:|delete_token:|confirm_delete:|cancel_delete)')
        ],
        states={
            TOKEN_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token_name_input)
            ],
            VALIDITY_PERIOD_SELECT: [
                CallbackQueryHandler(handle_token_callback_query, pattern=r'^validity:')
            ]
        },
        fallbacks=get_global_fallbacks(),
        per_chat=True,
        per_user=True,
        allow_reentry=True,
        persistent=False
    )
    