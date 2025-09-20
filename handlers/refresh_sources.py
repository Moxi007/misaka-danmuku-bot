import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from utils.api import call_danmaku_api
from utils.permission import check_admin_permission
from handlers.import_url import search_video_by_keyword
from utils.rate_limit import should_block_by_rate_limit

logger = logging.getLogger(__name__)

# 状态常量
REFRESH_KEYWORD_INPUT, REFRESH_ANIME_SELECT, REFRESH_SOURCE_SELECT, REFRESH_EPISODE_INPUT = range(4)

@check_admin_permission
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/refresh命令"""
    # 检查流控状态
    should_block, seconds_until_reset = should_block_by_rate_limit()
    if should_block:
        seconds_text = str(seconds_until_reset) if seconds_until_reset is not None else "稍后"
        await update.message.reply_text(f"🚫 无法执行刷新，当前系统处于流控状态，请{seconds_text}秒后再试")
        return ConversationHandler.END
        
    # 获取命令参数
    args = context.args
    
    if args:
        # 有参数，直接使用关键词搜索
        keyword = ' '.join(args)
        context.user_data['refresh_keyword'] = keyword
        return await process_refresh_search(update, context, keyword)
    else:
        # 无参数，提示输入关键词并提供从弹幕库选择的选项
        keyboard = [
            [InlineKeyboardButton("📚 从弹幕库中选择", callback_data="refresh_from_library")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🔄 **数据源刷新**\n\n"
            "请输入要刷新的影视关键词：\n\n"
            "💡 或者点击下方按钮从弹幕库中选择",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        return REFRESH_KEYWORD_INPUT

async def handle_refresh_keyword_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理刷新关键词输入"""
    keyword = update.message.text.strip()
    
    if not keyword:
        await update.message.reply_text("❌ 关键词不能为空，请重新输入：")
        return REFRESH_KEYWORD_INPUT
    
    context.user_data['refresh_keyword'] = keyword
    return await process_refresh_search(update, context, keyword)

async def process_refresh_search(update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str):
    """处理刷新搜索逻辑"""
    try:
        # 搜索匹配的影视
        matches = search_video_by_keyword(keyword)
        
        if not matches:
            await update.message.reply_text(
                f"❌ 未找到包含关键词 '{keyword}' 的影视\n\n"
                "请重新输入关键词："
            )
            return REFRESH_KEYWORD_INPUT
        
        # 保存搜索结果到上下文
        context.user_data['refresh_anime_matches'] = matches
        
        if len(matches) == 1:
            # 只有一个匹配结果，直接进入源选择
            anime = matches[0]
            context.user_data['refresh_selected_anime'] = anime
            return await show_refresh_sources(update, context, anime)
        else:
            # 多个匹配结果，让用户选择
            return await show_refresh_anime_selection(update, context, matches)
            
    except Exception as e:
        logger.error(f"处理刷新搜索时发生错误: {e}")
        await update.message.reply_text("❌ 搜索时发生错误，请稍后重试")
        return ConversationHandler.END

async def show_refresh_anime_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, matches):
    """显示刷新影视选择列表"""
    message = f"🔍 找到 {len(matches)} 个匹配结果：\n\n"
    
    for i, anime in enumerate(matches, 1):
        title = anime.get('title', '未知标题')
        year = anime.get('year', '')
        season = anime.get('season', '')
        episode_count = anime.get('episodeCount', 0)
        
        info = f"{title}"
        if year:
            info += f" ({year})"
        if season:
            info += f" 第{season}季"
        if episode_count:
            info += f" [{episode_count}集]"
        
        message += f"{i}. {info}\n"
    
    message += "\n请输入序号选择影视："
    
    await update.message.reply_text(message)
    return REFRESH_ANIME_SELECT

async def handle_refresh_anime_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理刷新影视选择"""
    user_input = update.message.text.strip()
    
    # 检查是否输入了'all'，给出更明确的提示
    if user_input.lower() == 'all':
        await update.message.reply_text(
            "❌ 请先选择要刷新的影视，然后在分集选择时才能使用 'all'\n\n"
            "请输入上方列表中的数字序号来选择影视："
        )
        return REFRESH_ANIME_SELECT
    
    try:
        selection = int(user_input)
        matches = context.user_data.get('refresh_anime_matches', [])
        
        if 1 <= selection <= len(matches):
            selected_anime = matches[selection - 1]
            context.user_data['refresh_selected_anime'] = selected_anime
            return await show_refresh_sources(update, context, selected_anime)
        else:
            await update.message.reply_text(
                f"❌ 请输入有效的序号 (1-{len(matches)})"
            )
            return REFRESH_ANIME_SELECT
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入数字序号"
        )
        return REFRESH_ANIME_SELECT

async def show_refresh_sources(update: Update, context: ContextTypes.DEFAULT_TYPE, anime):
    """显示刷新源选择"""
    anime_id = anime.get('animeId')
    title = anime.get('title', '未知影视')
    
    try:
        # 调用API获取源列表
        response = call_danmaku_api('GET', f'/library/anime/{anime_id}/sources')
        
        if not response or not response.get('success'):
            await update.message.reply_text(
                "❌ 获取影视源失败，请稍后重试"
            )
            return ConversationHandler.END
        
        sources = response.get('data', [])
        
        # 调试：检查源数据结构
        logger.info(f"获取到源数据：总数={len(sources)}")
        if sources:
            sample_source = sources[0]
            logger.info(f"源数据样例：{sample_source}")
            logger.info(f"源数据字段：{list(sample_source.keys())}")
        
        if not sources:
            await update.message.reply_text(
                "❌ 该影视暂无可用源"
            )
            return ConversationHandler.END
        
        # 保存源列表到上下文
        context.user_data['refresh_anime_sources'] = sources
        
        if len(sources) == 1:
            # 只有一个源，直接显示刷新选项
            source = sources[0]
            return await show_refresh_options(update, context, anime, source)
        else:
            # 多个源，让用户选择
            return await show_refresh_source_selection(update, context, anime, sources)
            
    except Exception as e:
        logger.error(f"获取刷新源异常: {e}")
        # 判断是回调查询还是普通消息
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.answer("❌ 获取影视源时发生错误，请稍后重试")
        else:
            await update.message.reply_text(
                "❌ 获取影视源时发生错误，请稍后重试"
            )
        return ConversationHandler.END

async def show_refresh_source_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, anime, sources):
    """显示刷新源选择列表"""
    title = anime.get('title', '未知影视')
    message = f"📺 {title}\n\n找到 {len(sources)} 个可用源：\n\n"
    
    for i, source in enumerate(sources, 1):
        source_name = source.get('providerName', f'源{i}')
        episode_count = source.get('episodeCount', 0)
        
        info = f"{source_name}"
        if episode_count:
            info += f" [{episode_count}集]"
        
        message += f"{i}. {info}\n"
    
    message += "\n请输入序号选择要刷新的源："
    
    await update.message.reply_text(message)
    return REFRESH_SOURCE_SELECT

async def handle_refresh_source_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理刷新源选择"""
    try:
        selection = int(update.message.text.strip())
        sources = context.user_data.get('refresh_anime_sources', [])
        
        # 调试：检查context中的源数据
        logger.info(f"处理源选择：输入={selection}, 源列表长度={len(sources)}")
        if sources:
            logger.info(f"源列表样例：{sources[0]}")
        else:
            logger.warning("源列表为空！检查context数据")
            logger.info(f"当前context.user_data keys: {list(context.user_data.keys())}")
        
        if 1 <= selection <= len(sources):
            selected_source = sources[selection - 1]
            anime = context.user_data.get('refresh_selected_anime')
            return await show_refresh_options(update, context, anime, selected_source)
        else:
            await update.message.reply_text(
                f"❌ 请输入有效的序号 (1-{len(sources)})"
            )
            return REFRESH_SOURCE_SELECT
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入数字序号"
        )
        return REFRESH_SOURCE_SELECT

async def show_refresh_options(update: Update, context: ContextTypes.DEFAULT_TYPE, anime, source):
    """显示刷新选项 - 获取并显示分集列表"""
    title = anime.get('title', '未知影视')
    source_name = source.get('providerName', '未知源')
    source_id = source.get('sourceId')
    
    try:
        # 调用API获取分集列表
        response = call_danmaku_api('GET', f'/library/source/{int(source_id)}/episodes')
        
        if not response or not response.get('success'):
            await update.message.reply_text(
                "❌ 获取分集列表失败，请稍后重试"
            )
            return ConversationHandler.END
        
        episodes = response.get('data', [])
        
        # 调试：检查媒体库API返回的数据结构
        logger.info(f"媒体库API返回分集数据：总数={len(episodes)}")
        if episodes:
            sample_episode = episodes[0]
            logger.info(f"媒体库API分集数据样例：{sample_episode}")
            logger.info(f"媒体库API分集字段：{list(sample_episode.keys())}")
        
        if not episodes:
            await update.message.reply_text(
                "❌ 该数据源暂无分集信息"
            )
            return ConversationHandler.END
        
        # 保存分集数据到上下文
        context.user_data['refresh_episodes'] = episodes
        context.user_data['refresh_source_info'] = {
            'source_id': source_id,
            'source_name': source_name,
            'anime_title': title
        }
        
        # 显示分集列表（第一页）
        return await show_episode_list(update, context, episodes, 0)
        
    except Exception as e:
        logger.error(f"获取分集列表异常: {e}")
        # 判断是回调查询还是普通消息
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.answer("❌ 获取分集列表时发生错误，请稍后重试")
        else:
            await update.message.reply_text(
                "❌ 获取分集列表时发生错误，请稍后重试"
            )
        return ConversationHandler.END

async def show_episode_list(update: Update, context: ContextTypes.DEFAULT_TYPE, episodes, page=0):
    """显示分集列表"""
    source_info = context.user_data.get('refresh_source_info', {})
    source_name = source_info.get('source_name', '未知源')
    anime_title = source_info.get('anime_title', '未知影视')
    
    # 转义Markdown特殊字符
    anime_title = anime_title.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[').replace(']', '\\]')
    source_name = source_name.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[').replace(']', '\\]')
    
    # 分页设置
    episodes_per_page = 10
    total_episodes = len(episodes)
    total_pages = (total_episodes + episodes_per_page - 1) // episodes_per_page
    
    if page < 0:
        page = 0
    elif page >= total_pages:
        page = total_pages - 1
    
    start_idx = page * episodes_per_page
    end_idx = min(start_idx + episodes_per_page, total_episodes)
    page_episodes = episodes[start_idx:end_idx]
    
    # 构建消息，避免Markdown解析错误
    message = (
        f"🔄 *分集刷新*\n\n"
        f"📺 影视：{anime_title}\n"
        f"🎯 数据源：{source_name}\n"
        f"📊 总计：{total_episodes} 集\n\n"
        f"*第 {page + 1}/{total_pages} 页*\n\n"
    )
    
    for episode in page_episodes:
        episode_index = episode.get('episodeIndex', 0)
        title = episode.get('title', f'第{episode_index}集')
        # 转义Markdown特殊字符
        title = title.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[').replace(']', '\\]')
        comment_count = episode.get('commentCount', 0)
        
        message += f"*{episode_index}.* {title}"
        if comment_count > 0:
            message += f" ({comment_count}条弹幕)"
        message += "\n"
    
    message += "\n💡 *刷新方式：*\n"
    message += "• 输入单个集数：如 `5`\n"
    message += "• 输入集数区间：如 `1-10` 或 `5,8,12`\n"
    message += "• 输入 `all` 刷新全部分集"
    
    # 构建按钮
    keyboard = []
    
    # 分页按钮
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"refresh_episodes_page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"refresh_episodes_page_{page+1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    # 判断是消息还是回调查询
    if hasattr(update, 'callback_query') and update.callback_query:
        # 来自回调查询，编辑消息
        await update.callback_query.edit_message_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        # 来自普通消息，回复消息
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    return REFRESH_EPISODE_INPUT

async def handle_refresh_episode_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理分集刷新输入"""
    user_input = update.message.text.strip()
    episodes = context.user_data.get('refresh_episodes', [])
    source_info = context.user_data.get('refresh_source_info', {})
    
    if not episodes or not source_info:
        await update.message.reply_text("❌ 会话数据丢失，请重新开始")
        return ConversationHandler.END
    
    try:
        # 解析用户输入
        episode_ids = parse_episode_input(user_input, episodes)
        
        if not episode_ids:
            await update.message.reply_text(
                "❌ 输入格式错误，请重新输入\n\n"
                "支持格式：\n"
                "• 单个集数：如 `5`\n"
                "• 集数区间：如 `1-10`\n"
                "• 多个集数：如 `5,8,12`\n"
                "• 全部集数：`all`"
            )
            return REFRESH_EPISODE_INPUT
        
        # 执行刷新
        return await execute_episode_refresh(update, context, episode_ids)
        
    except Exception as e:
        logger.error(f"处理分集刷新输入异常: {e}")
        await update.message.reply_text("❌ 处理输入时发生错误，请重新输入")
        return REFRESH_EPISODE_INPUT

def parse_episode_input(user_input: str, episodes):
    """解析用户输入的集数"""
    episode_ids = []
    episode_map = {ep.get('episodeIndex'): ep.get('episodeId') for ep in episodes if ep.get('episodeId')}
    
    if user_input.lower() == 'all':
        # 刷新全部，只返回有效的episodeId
        all_episode_ids = [ep.get('episodeId') for ep in episodes if ep.get('episodeId')]
        logger.info(f"解析'all'输入：总集数={len(episodes)}, 有效episodeId数量={len(all_episode_ids)}, episodeIds={all_episode_ids}")
        return all_episode_ids
    
    # 处理逗号分隔的多个输入
    parts = [part.strip() for part in user_input.split(',')]
    
    for part in parts:
        if '-' in part:
            # 处理区间 如 1-10
            try:
                start, end = map(int, part.split('-', 1))
                for i in range(start, end + 1):
                    if i in episode_map:
                        episode_ids.append(episode_map[i])
            except ValueError:
                return []
        else:
            # 处理单个集数
            try:
                episode_index = int(part)
                if episode_index in episode_map:
                    episode_ids.append(episode_map[episode_index])
            except ValueError:
                return []
    
    return list(set(episode_ids))  # 去重

async def execute_episode_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE, episode_ids):
    """执行分集刷新"""
    source_info = context.user_data.get('refresh_source_info', {})
    anime_title = source_info.get('anime_title', '未知影视')
    source_name = source_info.get('source_name', '未知源')
    
    # 转义Markdown特殊字符
    anime_title = anime_title.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[').replace(']', '\\]')
    source_name = source_name.replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[').replace(']', '\\]')
    
    total_count = len(episode_ids)
    logger.info(f"开始执行分集刷新：影视={anime_title}, 数据源={source_name}, 集数={total_count}, episodeIds={episode_ids}")
    
    await update.message.reply_text(
        f"🔄 *开始刷新分集*\n\n"
        f"📺 影视：{anime_title}\n"
        f"🎯 数据源：{source_name}\n"
        f"📊 刷新集数：{total_count} 集\n\n"
        f"⏳ 正在处理中，请稍候...",
        parse_mode='Markdown'
    )
    
    success_count = 0
    failed_count = 0
    task_ids = []
    
    for episode_id in episode_ids:
        try:
            logger.info(f"正在刷新分集 {episode_id}...")
            response = call_danmaku_api('POST', f'/library/episode/{episode_id}/refresh')
            logger.info(f"分集 {episode_id} API响应: {response}")
            
            if response and response.get('success'):
                success_count += 1
                data = response.get('data', {})
                task_id = data.get('taskId')
                logger.info(f"📊 响应data字段: {data}")
                if task_id:
                    task_ids.append(task_id)
                logger.info(f"分集 {episode_id} 刷新成功，taskId: {task_id}")
            else:
                failed_count += 1
                logger.warning(f"分集 {episode_id} 刷新失败，API响应: {response}")
                
        except Exception as e:
            logger.error(f"刷新分集 {episode_id} 失败: {e}")
            failed_count += 1
    
    # 显示结果
    result_message = (
        f"✅ *分集刷新完成*\n\n"
        f"📺 影视：{anime_title}\n"
        f"🎯 数据源：{source_name}\n\n"
        f"📊 *刷新统计：*\n"
        f"• 成功：{success_count} 集\n"
        f"• 失败：{failed_count} 集\n"
        f"• 总计：{total_count} 集\n\n"
    )
    
    if task_ids:
        result_message += f"🔗 {len(task_ids)} 个刷新任务已提交"
    
    await update.message.reply_text(result_message, parse_mode='Markdown')
    
    return ConversationHandler.END

async def cancel_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消刷新操作"""
    # 清理用户数据
    keys_to_remove = [
        'refresh_keyword', 'refresh_anime_matches', 'refresh_selected_anime', 'refresh_anime_sources'
    ]
    for key in keys_to_remove:
        context.user_data.pop(key, None)
    
    await update.message.reply_text("❌ 刷新操作已取消")
    return ConversationHandler.END

async def handle_refresh_from_library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理从弹幕库中选择的回调"""
    query = update.callback_query
    await query.answer()
    
    # 显示加载状态
    await query.edit_message_text("🔄 正在加载弹幕库数据...")
    
    try:
        # 获取库数据并显示库列表供选择
        return await show_library_selection(update, context)
        
    except Exception as e:
        logger.error(f"处理弹幕库选择时发生错误: {e}")
        await query.edit_message_text("❌ 获取弹幕库数据时发生错误，请稍后重试")
        return ConversationHandler.END

async def show_library_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    """显示弹幕库列表供选择"""
    try:
        # 调用/library接口获取所有库数据
        response = call_danmaku_api('GET', '/library')
        if not response or 'data' not in response:
            await update.callback_query.edit_message_text("❌ 获取弹幕库数据失败，请稍后重试")
            return ConversationHandler.END
        
        library_data = response['data']
        if not library_data:
            await update.callback_query.edit_message_text("📚 弹幕库为空")
            return ConversationHandler.END
            
    except Exception as e:
        logger.error(f"获取库数据失败: {e}")
        await update.callback_query.edit_message_text("❌ 获取弹幕库数据失败，请稍后重试")
        return ConversationHandler.END
    
    items_per_page = 10
    total_items = len(library_data)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)
    page_items = library_data[start_idx:end_idx]
    
    # 构建消息文本
    message_text = f"📚 **弹幕库列表** (第 {page + 1}/{total_pages} 页)\n\n"
    
    # 构建按钮
    keyboard = []
    for i, anime in enumerate(page_items):
        title = anime.get('title', '未知标题')
        # 限制标题长度避免按钮过长
        if len(title) > 25:
            title = title[:22] + "..."
        
        callback_data = f"refresh_select_anime_{start_idx + i}"
        keyboard.append([InlineKeyboardButton(title, callback_data=callback_data)])
    
    # 添加分页按钮
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"refresh_library_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"refresh_library_page_{page + 1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    # 添加取消按钮
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="refresh_cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    query = update.callback_query
    if query:
        await query.edit_message_text(
            text=message_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            text=message_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    return REFRESH_ANIME_SELECT

def create_refresh_handler():
    """创建刷新命令处理器"""
    from callback.refresh_sources import handle_refresh_callback_query
    
    return ConversationHandler(
        entry_points=[
            CommandHandler('refresh', refresh_command)
        ],
        states={
            REFRESH_KEYWORD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_refresh_keyword_input),
                CallbackQueryHandler(handle_refresh_from_library, pattern='^refresh_from_library$')
            ],
            REFRESH_ANIME_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_refresh_anime_selection),
                CallbackQueryHandler(handle_refresh_callback_query, pattern=r'^refresh_select_anime_\d+$'),
                CallbackQueryHandler(handle_refresh_callback_query, pattern=r'^refresh_library_page_\d+$'),
                CallbackQueryHandler(handle_refresh_callback_query, pattern='^refresh_cancel$')
            ],
            REFRESH_SOURCE_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_refresh_source_selection)
            ],
            REFRESH_EPISODE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_refresh_episode_input),
                CallbackQueryHandler(handle_refresh_callback_query, pattern=r'^refresh_episodes_page_\d+$')
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_refresh)
        ],
        allow_reentry=True
    )