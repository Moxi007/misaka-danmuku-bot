import logging
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from utils.api import call_danmaku_api
from utils.permission import check_user_permission
from utils.url_parser import determine_input_type
from utils.tmdb_api import get_media_type_suggestion, format_tmdb_results_info, format_tmdb_media_info, get_tmdb_tv_seasons, get_tmdb_media_details
from utils.tvdb_api import search_tvdb_by_slug, get_tvdb_tv_seasons
from utils.imdb_scraper import get_imdb_info
from utils.bgm_scraper import get_bgm_info
from utils.emby_name_converter import convert_emby_series_name
from utils.rate_limit import should_block_by_rate_limit
from utils.handlers_utils import wrap_conversation_entry_point, wrap_with_session_management
from utils.handlers_fallbacks import get_global_fallbacks
from callback.import_media import handle_get_episode_callback, handle_episode_range_input, cancel_episode_input, handle_search_type_callback, handle_media_type_callback, handle_import_auto_callback
from handlers.general import cancel_command
from utils.conversation_states import SEARCH_MEDIA, SEARCH_RESULTS, INPUT_EPISODE_RANGE, IMPORT_AUTO_SEARCH_TYPE, IMPORT_AUTO_KEYWORD_INPUT, IMPORT_AUTO_ID_INPUT, IMPORT_AUTO_SEASON_SELECTION, CALLBACK_DATA_MAX_LEN, EPISODES_PER_PAGE

# 初始化日志
logger = logging.getLogger(__name__)


@check_user_permission
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """搜索媒体：支持直接带关键词或后续输入"""
    # 检查流控状态
    should_block, seconds_until_reset = should_block_by_rate_limit()
    if should_block:
        seconds_text = str(seconds_until_reset) if seconds_until_reset is not None else "稍后"
        await update.message.reply_text(f"🚫 无法搜索，当前系统处于流控状态，请{seconds_text}秒后再试")
        return ConversationHandler.END
    
    # 1. 直接带参数（如：/search 海贼王）
    if context.args:
        keyword = " ".join(context.args)
        return await process_search_media(update, keyword, context)

    # 2. 无参数：引导用户输入关键词
    await update.message.reply_text("请输入要搜索的媒体关键词（如：海贼王、进击的巨人）：")
    return SEARCH_MEDIA


async def search_media_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收用户输入的搜索关键词"""
    keyword = update.message.text.strip()
    if not keyword:
        await update.message.reply_text("❌ 关键词不能为空，请重新输入：")
        return SEARCH_MEDIA

    return await process_search_media(update, keyword, context)


async def process_search_media(update: Update, keyword: str, context: ContextTypes.DEFAULT_TYPE):
    """处理搜索逻辑：生成带「导入按钮」的结果列表"""
    await update.message.reply_text(f"🔍 正在搜索关键词「{keyword}」...")
    
    # 1. 调用API搜索
    api_result = call_danmaku_api(
        method="GET",
        endpoint="/search",
        params={"keyword": keyword}
    )

    # 2. 处理API响应
    if not api_result["success"]:
        await update.message.reply_text(f"❌ 搜索失败：{api_result['error']}")
        return
    search_data = api_result["data"]
    search_id = search_data.get("searchId", "")
    items = search_data.get("results", [])
    
    if not search_id:
        await update.message.reply_text("❌ 搜索结果缺少searchId，无法后续导入")
        return
    if not items:
        await update.message.reply_text(f"❌ 未找到关键词「{keyword}」的媒体")
        return

    # 3. 保存searchId到上下文（供后续导入使用）
    context.user_data["search_id"] = search_id
    
    # 4. 保存搜索结果到上下文
    context.user_data["search_results"] = items
    
    # 5. 直接显示分页结果（每页5条）
    from callback.import_media import show_paged_results
    await show_paged_results(update, context, items, page=0, per_page=5)
    
    # 返回搜索结果状态，保持对话继续
    return SEARCH_RESULTS


@check_user_permission
async def auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """自动导入命令：支持直接带参数或显示选择界面"""
    # 检查流控状态
    should_block, seconds_until_reset = should_block_by_rate_limit()
    if should_block:
        seconds_text = str(seconds_until_reset) if seconds_until_reset is not None else "稍后"
        await update.message.reply_text(f"🚫 无法自动导入，当前系统处于流控状态，请{seconds_text}秒后再试")
        return ConversationHandler.END
    
    # 检查是否有参数
    if context.args:
        # 有参数：直接处理输入
        input_text = " ".join(context.args)
        return await process_auto_input(update, context, input_text)
    
    # 无参数：显示searchType选择界面
    keyboard = [
        [InlineKeyboardButton("🔍 关键词搜索", callback_data=json.dumps({"action": "import_auto_search_type", "type": "keyword"}, ensure_ascii=False))],
        [InlineKeyboardButton("🎬 TMDB ID", callback_data=json.dumps({"action": "import_auto_search_type", "type": "tmdb"}, ensure_ascii=False))],
        [InlineKeyboardButton("📺 TVDB ID", callback_data=json.dumps({"action": "import_auto_search_type", "type": "tvdb"}, ensure_ascii=False))],
        [InlineKeyboardButton("🎭 豆瓣 ID", callback_data=json.dumps({"action": "import_auto_search_type", "type": "douban"}, ensure_ascii=False))],
        [InlineKeyboardButton("🌟 IMDB ID", callback_data=json.dumps({"action": "import_auto_search_type", "type": "imdb"}, ensure_ascii=False))],
        [InlineKeyboardButton("🎯 Bangumi ID", callback_data=json.dumps({"action": "import_auto_search_type", "type": "bangumi"}, ensure_ascii=False))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 **自动导入功能**\n\n"
        "请选择搜索方式：\n\n"
        "• 🔍 **关键词搜索**：输入影视名称，TMDB智能识别类型和季度\n"
        "• 🆔 **平台ID**：直接使用各平台的ID或链接，支持季度选择\n\n"
        "💡 **快速导入**：直接使用 `/auto 关键词` 或 `/auto 平台链接`",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    
    # 返回状态1，等待搜索类型选择
    return 1


async def process_media_input_unified(update: Update, context: ContextTypes.DEFAULT_TYPE, input_info: dict, input_text: str):
    """统一处理各种媒体输入类型"""
    if input_info["type"] == "tmdb_url":
        return await process_tmdb_input(update, context, input_info)
    elif input_info["type"] == "tvdb_url":
        return await process_tvdb_input(update, context, input_info, input_text)
    elif input_info["type"] == "douban_url":
        return await process_douban_input(update, context, input_info)
    elif input_info["type"] == "imdb_url":
       return await process_imdb_input(update, context, input_info)
    elif input_info["type"] == "bgm_url":
        return await process_bgm_input(update, context, input_info)
    elif input_info["type"] == "keyword":
        # 检查是否在ID模式下输入了纯数字ID或TVDB格式ID
        search_type = context.user_data.get("import_auto_search_type")
        input_stripped = input_text.strip()
        
        # 判断是否为平台ID格式
        is_platform_id = False
        if search_type:
            # 检查输入是否包含英文、字符和数字的组合（排除纯中文等明显的关键词）
            # 使用正则表达式匹配英文字母、数字、常见符号的组合
            import re
            # 匹配包含字母、数字、连字符、下划线等常见ID字符的组合
            id_pattern = re.compile(r'^[a-zA-Z0-9\-_]+$')
            is_platform_id = bool(id_pattern.match(input_stripped)) and len(input_stripped) > 0
        
        if search_type and is_platform_id:
            # 在ID模式下输入平台ID格式，按照平台ID处理
            return await process_platform_id(update, context, input_stripped, search_type)
        else:
            # 处理关键词搜索
            return await process_keyword_search(update, context, input_text)
    else:
        # 处理关键词搜索
        return await process_keyword_search(update, context, input_text)


async def process_platform_id(update: Update, context: ContextTypes.DEFAULT_TYPE, platform_id: str, search_type: str):
    """处理平台纯数字ID输入
    
    Args:
        update: Telegram更新对象
        context: 上下文对象
        platform_id: 平台ID（纯数字）
        search_type: 搜索平台类型（tmdb/tvdb/imdb/douban/bangumi）
    """
    if search_type == "tvdb":
        # 检查TVDB API是否启用
        from config import ConfigManager
        config_manager = ConfigManager()
        if not config_manager.tvdb.enabled:
            # API未启用，跳过智能处理，使用默认电视剧类型
            logger.info("ℹ️ TVDB API未启用，跳过智能解析，使用默认电视剧类型")
            await update.message.reply_text(
                f"🆔 **TVDB ID: {platform_id}**\n\n"
                "⚠️ TVDB API未配置，将使用默认电视剧类型处理"
            )
            
            # 构建默认导入参数（电视剧类型）
            import_params = {
                "searchType": "tvdb",
                "searchTerm": platform_id,
                "mediaType": "tv_series",
                "importMethod": "auto",
                "originalKeyword": f"TVDB ID: {platform_id}"
            }
            
            # 统一处理
            return await handle_media_import_flow(update, context, import_params)
        
        # TVDB ID，先尝试智能解析获取详细信息
        await update.message.reply_text(f"🆔 **TVDB ID: {platform_id}**\n\n🔍 正在查询TVDB API获取详细信息...")
        
        # 尝试通过API获取详细信息来智能判断类型
        from utils.tvdb_api import search_tvdb_by_slug
        
        # 先尝试电视剧类型
        tv_result = None
        movie_result = None
        detected_type = None
        
        try:
            tv_result = await search_tvdb_by_slug(platform_id, "tv_series")
            if tv_result and "tvdb_id" in tv_result:
                detected_type = "tv_series"
                await update.message.reply_text("📺 智能识别为电视剧类型")
        except Exception as e:
            logger.debug(f"TVDB电视剧类型查询失败: {e}")
        
        # 如果电视剧查询失败，尝试电影类型
        if not detected_type:
            try:
                movie_result = await search_tvdb_by_slug(platform_id, "movie")
                if movie_result and "tvdb_id" in movie_result:
                    detected_type = "movie"
                    await update.message.reply_text("🎬 智能识别为电影类型")
            except Exception as e:
                logger.debug(f"TVDB电影类型查询失败: {e}")
        
        if detected_type:
            # 智能识别成功，显示详细信息并处理
            tvdb_result = tv_result if detected_type == "tv_series" else movie_result
            
            # 提取数字ID部分（去掉series-前缀）
            raw_tvdb_id = tvdb_result["tvdb_id"]
            if raw_tvdb_id.startswith("series-"):
                tvdb_id = raw_tvdb_id.replace("series-", "")
            elif raw_tvdb_id.startswith("movie-"):
                tvdb_id = raw_tvdb_id.replace("movie-", "")
            else:
                tvdb_id = str(raw_tvdb_id)
            
            title = tvdb_result.get("name", "未知标题")
            
            type_icon = "📺" if detected_type == 'tv_series' else "🎬"
            type_name = "电视剧" if detected_type == 'tv_series' else "电影"
            
            # 根据媒体类型显示不同的提示信息
            if detected_type == "movie":
                status_text = "🚀 开始自动导入..."
            else:
                status_text = "🔍 正在获取季度信息..."
            
            await update.message.reply_text(
                f"✅ **TVDB查询成功**\n\n"
                f"🎬 标题: {title}\n"
                f"🆔 ID: `{tvdb_id}`\n"
                f"{type_icon} 类型: {type_name}\n\n"
                f"{status_text}",
                parse_mode="Markdown"
            )
            
            # 构建导入参数
            import_params = {
                "searchType": "tvdb",
                "searchTerm": tvdb_id,
                "mediaType": detected_type,
                "importMethod": "auto",
                "originalKeyword": title
            }
            
            # 统一处理电影和电视剧
            return await handle_media_import_flow(update, context, import_params)
        else:
            # 智能识别失败，使用默认电视剧类型
            await update.message.reply_text(
                f"⚠️ **无法自动识别媒体类型，使用默认电视剧类型**\n\n"
                f"🚀 开始导入..."
            )
            
            # 构建默认导入参数（电视剧类型）
            import_params = {
                "searchType": "tvdb",
                "searchTerm": platform_id,
                "mediaType": "tv_series",
                "importMethod": "auto",
                "originalKeyword": f"TVDB ID: {platform_id}"
            }
            
            # 统一处理
            return await handle_media_import_flow(update, context, import_params)
    
    elif search_type == "tmdb":
        # 检查TMDB API是否启用
        from config import ConfigManager
        config_manager = ConfigManager()
        if not config_manager.tmdb.enabled:
            # API未启用，跳过智能处理，显示手动选择按钮
            logger.info("ℹ️ TMDB API未启用，跳过智能解析，显示手动选择")
            await update.message.reply_text(
                f"🆔 **TMDB ID: {platform_id}**\n\n"
                "⚠️ TMDB API未配置，请手动选择媒体类型："
            )
            
            # 保存ID到上下文
            context.user_data["import_auto_id"] = platform_id
            
            # 显示媒体类型选择按钮
            keyboard = [
                [InlineKeyboardButton("📺 电视剧/动漫", callback_data=json.dumps({"action": "import_auto_media_type", "type": "tv_series"}, ensure_ascii=False))],
                [InlineKeyboardButton("🎬 电影", callback_data=json.dumps({"action": "import_auto_media_type", "type": "movie"}, ensure_ascii=False))]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "请选择媒体类型：",
                reply_markup=reply_markup
            )
            
            return IMPORT_AUTO_ID_INPUT  # 等待媒体类型选择
        
        # TMDB ID需要媒体类型，但先尝试智能解析获取详细信息
        await update.message.reply_text(f"🆔 **TMDB ID: {platform_id}**\n\n🔍 正在获取TMDB媒体信息...")
        
        # 尝试获取媒体详细信息来智能判断类型
        from utils.tmdb_api import get_tmdb_media_details, format_tmdb_media_info
        
        # 先尝试电视剧类型
        tv_details = None
        movie_details = None
        detected_type = None
        
        try:
            tv_details = get_tmdb_media_details(platform_id, "tv_series")
            if tv_details and tv_details.get('id'):
                detected_type = "tv_series"
                await update.message.reply_text("📺 智能识别为电视剧类型")
                detailed_info = format_tmdb_media_info(platform_id, "tv_series")
                await update.message.reply_text(detailed_info)
        except Exception as e:
            logger.debug(f"TMDB电视剧类型查询失败: {e}")
        
        # 如果电视剧查询失败，尝试电影类型
        if not detected_type:
            try:
                movie_details = get_tmdb_media_details(platform_id, "movie")
                if movie_details and movie_details.get('id'):
                    detected_type = "movie"
                    await update.message.reply_text("🎬 智能识别为电影类型")
                    detailed_info = format_tmdb_media_info(platform_id, "movie")
                    await update.message.reply_text(detailed_info)
            except Exception as e:
                logger.debug(f"TMDB电影类型查询失败: {e}")
        
        if detected_type:
            # 智能识别成功，直接处理
            original_keyword = None
            media_details = tv_details if detected_type == "tv_series" else movie_details
            if media_details:
                original_keyword = media_details.get('title') or media_details.get('name')
            
            if not original_keyword:
                original_keyword = f"TMDB ID: {platform_id}"
            
            # 构建导入参数
            import_params = {
                "searchType": "tmdb",
                "searchTerm": platform_id,
                "mediaType": detected_type,
                "importMethod": "auto",
                "originalKeyword": original_keyword
            }
            
            # 统一处理电影和电视剧
            return await handle_media_import_flow(update, context, import_params)
        else:
            # 智能识别失败，显示手动选择按钮
            await update.message.reply_text(
                f"⚠️ **无法自动识别媒体类型**\n\n"
                f"请手动选择媒体类型："
            )
            
            # 保存ID到上下文
            context.user_data["import_auto_id"] = platform_id
            
            # 显示媒体类型选择按钮
            keyboard = [
                [InlineKeyboardButton("📺 电视剧/动漫", callback_data=json.dumps({"action": "import_auto_media_type", "type": "tv_series"}, ensure_ascii=False))],
                [InlineKeyboardButton("🎬 电影", callback_data=json.dumps({"action": "import_auto_media_type", "type": "movie"}, ensure_ascii=False))]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "请选择媒体类型：",
                reply_markup=reply_markup
            )
            
            return IMPORT_AUTO_ID_INPUT  # 等待媒体类型选择
        
    elif search_type == "imdb":
        # IMDB纯数字ID，转换为tt格式并进行智能解析
        imdb_id = f"tt{platform_id}"
        await update.message.reply_text(f"🆔 **IMDB ID: {imdb_id}**\n\n🔍 正在获取IMDB媒体信息...")
        
        # 尝试获取IMDB详细信息进行智能解析
        from utils.imdb_scraper import get_imdb_info
        
        try:
            imdb_info = get_imdb_info(imdb_id)
            
            if imdb_info and imdb_info.get('success'):
                media_title = imdb_info.get('title', 'N/A')
                media_year = imdb_info.get('year', 'N/A')
                rating = imdb_info.get('rating', 'N/A')
                
                # 获取媒体类型
                media_type = imdb_info.get('media_type', 'movie')
                if media_type == 'tv_series':
                    type_name = '电视剧/动漫'
                    type_icon = '📺'
                else:
                    type_name = '电影'
                    type_icon = '🎬'
                
                await update.message.reply_text(
                    f"✅ **IMDB信息获取成功**\n\n"
                    f"🎬 标题: {media_title}\n"
                    f"📅 年份: {media_year}\n"
                    f"{type_icon} 类型: {type_name}\n"
                    f"⭐ 评分: {rating}\n\n",
                    parse_mode="Markdown"
                )
                
                # 构建导入参数
                import_params = {
                    "searchType": "imdb",
                    "searchTerm": imdb_id,
                    "mediaType": media_type,
                    "importMethod": "auto",
                    "originalKeyword": media_title
                }
                
                # 统一处理电影和电视剧
                return await handle_media_import_flow(update, context, import_params)
            else:
                error_msg = imdb_info.get('error', '未知错误') if imdb_info else '网络请求失败'
                logger.warning(f"IMDB信息解析失败，直接使用ID导入: {error_msg}")
                await update.message.reply_text(
                    f"⚠️ **IMDB信息解析失败，将直接使用ID导入**\n\n"
                    f"📋 IMDB ID: `{imdb_id}`\n"
                    f"🔄 跳过详细信息获取，直接进行导入...",
                    parse_mode="Markdown"
                )
                
                # 使用默认参数
                modified_info = {
                    "type": "imdb_url",
                    "imdb_id": imdb_id
                }
                return await process_imdb_input(update, context, modified_info)
        except Exception as e:
            logger.error(f"IMDB爬虫异常: imdb_id='{imdb_id}', error={str(e)}")
            logger.warning(f"IMDB信息解析异常，直接使用ID导入: {str(e)}")
            await update.message.reply_text(
                f"⚠️ **IMDB信息解析异常，将直接使用ID导入**\n\n"
                f"📋 IMDB ID: `{imdb_id}`\n"
                f"🔄 跳过详细信息获取，直接进行导入...",
                parse_mode="Markdown"
            )
            
            # 使用默认参数
            modified_info = {
                "type": "imdb_url",
                "imdb_id": imdb_id
            }
            return await process_imdb_input(update, context, modified_info)
        
    elif search_type == "douban":
        # 豆瓣ID，进行智能解析
        await update.message.reply_text(f"🆔 **豆瓣ID: {platform_id}**\n\n🔍 正在获取豆瓣媒体信息...")
        
        # 尝试获取豆瓣详细信息进行智能解析
        try:
            # 构建豆瓣URL进行解析
            douban_url = f"https://movie.douban.com/subject/{platform_id}/"
            from utils.url_parser import parse_douban_url
            
            parsed_info = parse_douban_url(douban_url)
            if parsed_info:
                await update.message.reply_text("✅ 豆瓣ID验证成功")
                
                # 构建导入参数
                import_params = {
                    "searchType": "douban",
                    "searchTerm": platform_id,
                    "mediaType": "tv_series",  # 豆瓣默认电视剧类型
                    "importMethod": "auto",
                    "originalKeyword": f"豆瓣ID: {platform_id}"
                }
                
                # 统一处理
                return await handle_media_import_flow(update, context, import_params)
            else:
                await update.message.reply_text(
                    f"⚠️ **豆瓣ID验证失败，将直接使用ID导入**\n\n"
                    f"📋 豆瓣ID: `{platform_id}`\n"
                    f"🔄 跳过详细信息获取，直接进行导入...",
                    parse_mode="Markdown"
                )
                
                # 使用默认参数
                modified_info = {
                    "type": "douban_url",
                    "douban_id": platform_id
                }
                return await process_douban_input(update, context, modified_info)
        except Exception as e:
            logger.error(f"豆瓣ID解析异常: douban_id='{platform_id}', error={str(e)}")
            await update.message.reply_text(
                f"⚠️ **豆瓣信息解析异常，将直接使用ID导入**\n\n"
                f"📋 豆瓣ID: `{platform_id}`\n"
                f"🔄 跳过详细信息获取，直接进行导入...",
                parse_mode="Markdown"
            )
            
            # 使用默认参数
            modified_info = {
                "type": "douban_url",
                "douban_id": platform_id
            }
            return await process_douban_input(update, context, modified_info)
        
    elif search_type == "bangumi":
        # BGM ID，进行智能解析
        await update.message.reply_text(f"🆔 **BGM ID: {platform_id}**\n\n🔍 正在获取BGM媒体信息...")
        
        # 尝试获取BGM详细信息进行智能解析
        from utils.bgm_scraper import get_bgm_info
        
        try:
            bgm_info = get_bgm_info(platform_id)
            
            if bgm_info and bgm_info.get('success'):
                media_title = bgm_info.get('title', 'N/A')
                media_year = bgm_info.get('year', 'N/A')
                rating = bgm_info.get('rating', 'N/A')
                media_type = bgm_info.get('media_type', 'tv_series')
                
                type_name = '电视剧/动漫' if media_type == 'tv_series' else '电影'
                type_icon = '📺' if media_type == 'tv_series' else '🎬'
                
                await update.message.reply_text(
                    f"✅ **BGM信息获取成功**\n\n"
                    f"🎬 标题: {media_title}\n"
                    f"📅 年份: {media_year}\n"
                    f"{type_icon} 类型: {type_name}\n"
                    f"⭐ 评分: {rating}\n\n",
                    parse_mode="Markdown"
                )
                
                # 构建导入参数
                import_params = {
                    "searchType": "bangumi",
                    "searchTerm": platform_id,
                    "mediaType": media_type,
                    "importMethod": "auto",
                    "originalKeyword": media_title
                }
                
                # 统一处理
                return await handle_media_import_flow(update, context, import_params)
            else:
                error_msg = bgm_info.get('error', '未知错误') if bgm_info else '网络请求失败'
                logger.warning(f"BGM信息解析失败，直接使用ID导入: {error_msg}")
                await update.message.reply_text(
                    f"⚠️ **BGM信息解析失败，将直接使用ID导入**\n\n"
                    f"📋 BGM ID: `{platform_id}`\n"
                    f"🔄 跳过详细信息获取，直接进行导入...",
                    parse_mode="Markdown"
                )
                
                # 使用默认参数
                modified_info = {
                    "type": "bgm_url",
                    "bgm_id": platform_id
                }
                return await process_bgm_input(update, context, modified_info)
        except Exception as e:
            logger.error(f"BGM爬虫异常: bgm_id='{platform_id}', error={str(e)}")
            logger.warning(f"BGM信息解析异常，直接使用ID导入: {str(e)}")
            await update.message.reply_text(
                f"⚠️ **BGM信息解析异常，将直接使用ID导入**\n\n"
                f"📋 BGM ID: `{platform_id}`\n"
                f"🔄 跳过详细信息获取，直接进行导入...",
                parse_mode="Markdown"
            )
            
            # 使用默认参数
            modified_info = {
                "type": "bgm_url",
                "bgm_id": platform_id
            }
            return await process_bgm_input(update, context, modified_info)
        
    else:
        # 未知平台，回退到关键词搜索
        await update.message.reply_text(
            f"⚠️ **未知平台类型: {search_type}**\n\n"
            f"将使用关键词搜索模式处理: {platform_id}"
        )
        return await process_keyword_search(update, context, platform_id)


async def process_tmdb_input(update: Update, context: ContextTypes.DEFAULT_TYPE, input_info: dict):
    """处理TMDB链接输入"""
    tmdb_id = input_info["tmdb_id"]
    media_type = input_info["media_type"]
    
    # 第一步：立即显示检测结果
    type_icon = "📺" if media_type == 'tv_series' else "🎬"
    await update.message.reply_text(f"{type_icon} 检测到 TMDB {'电视剧' if media_type == 'tv_series' else '电影'}\n\n🆔 ID: {tmdb_id}")
    
    # 第二步：尝试获取并显示详细信息
    await update.message.reply_text("🔍 正在获取TMDB媒体信息...")
    
    # 尝试获取TMDB标题作为originalKeyword
    original_keyword = None
    try:
        detailed_info = format_tmdb_media_info(tmdb_id, media_type)
        await update.message.reply_text(detailed_info)
        
        # 从TMDB获取标题作为originalKeyword
        from utils.tmdb_api import get_tmdb_media_details
        media_details = get_tmdb_media_details(tmdb_id, media_type)
        if media_details:
            original_keyword = media_details.get('title') or media_details.get('name')
            
    except Exception as e:
        logger.warning(f"TMDB信息解析失败，直接使用ID导入: {e}")
        await update.message.reply_text(
            f"⚠️ **TMDB信息解析失败，将直接使用ID导入**\n\n"
            f"📋 TMDB ID: {tmdb_id}\n"
            f"🔄 跳过详细信息获取，直接进行导入..."
        )
    
    # 如果无法获取标题，使用TMDB ID作为fallback
    if not original_keyword:
        original_keyword = f"TMDB ID: {tmdb_id}"
    
    # 构建导入参数
    import_params = {
        "searchType": "tmdb",
        "searchTerm": tmdb_id,
        "mediaType": media_type,
        "importMethod": "auto",
        "originalKeyword": original_keyword
    }
    
    # 统一处理电影和电视剧
    return await handle_media_import_flow(update, context, import_params)

async def process_tvdb_input(update: Update, context: ContextTypes.DEFAULT_TYPE, input_info: dict, input_text: str):
    """处理TVDB链接输入"""
    media_type = input_info["media_type"]
    slug = input_info["slug"]
    
    # 域名验证
    from utils.url_parser import is_tvdb_url
    if not is_tvdb_url(input_text):
        await update.message.reply_text(
            "❌ **域名验证失败**\n\n"
            "请确保输入的是有效的TVDB链接：\n"
            "• https://www.thetvdb.com/series/xxx\n"
            "• https://www.thetvdb.com/movies/xxx",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    
    # 第一步：立即显示检测结果
    type_icon = "📺" if media_type == 'tv_series' else "🎬"
    await update.message.reply_text(f"{type_icon} 检测到 TVDB {'电视剧' if media_type == 'tv_series' else '电影'}")
    
    # 第二步：显示正在查询的状态
    await update.message.reply_text("🔍 正在查询TVDB API获取数字ID...")
    
    # 通过API获取数字ID
    tvdb_result = await search_tvdb_by_slug(slug, media_type)
    
    # 如果指定类型查询失败，尝试查询另一种类型作为回退
    if not tvdb_result or "tvdb_id" not in tvdb_result:
        fallback_type = "tv_series" if media_type == "movie" else "movie"
        await update.message.reply_text(f"⚠️ 未找到对应的{'电影' if media_type == 'movie' else '电视剧'}，尝试查询{'电视剧' if fallback_type == 'tv_series' else '电影'}...")
        tvdb_result = await search_tvdb_by_slug(slug, fallback_type)
        if tvdb_result and "tvdb_id" in tvdb_result:
            media_type = fallback_type  # 更新为实际找到的类型
            await update.message.reply_text(f"✅ 找到了{'电视剧' if media_type == 'tv_series' else '电影'}版本，将使用此类型进行导入")
    
    if tvdb_result and "tvdb_id" in tvdb_result:
        # 提取数字ID部分（去掉series-前缀）
        raw_tvdb_id = tvdb_result["tvdb_id"]
        if raw_tvdb_id.startswith("series-"):
            tvdb_id = raw_tvdb_id.replace("series-", "")
        elif raw_tvdb_id.startswith("movie-"):
            tvdb_id = raw_tvdb_id.replace("movie-", "")
        else:
            tvdb_id = str(raw_tvdb_id)
        title = tvdb_result.get("name", "未知标题")
        
        type_icon = "📺" if media_type == 'tv_series' else "🎬"
        type_name = "电视剧" if media_type == 'tv_series' else "电影"
        
        # 根据媒体类型显示不同的提示信息
        if media_type == "movie":
            status_text = "🚀 开始自动导入..."
        else:
            status_text = "🔍 正在获取季度信息..."
        
        await update.message.reply_text(
            f"✅ **TVDB查询成功**\n\n"
            f"🎬 标题: {title}\n"
            f"🆔 ID: `{tvdb_id}`\n"
            f"{type_icon} 类型: {type_name}\n\n"
            f"{status_text}",
            parse_mode="Markdown"
        )
        
        # 构建导入参数
        import_params = {
            "searchType": "tvdb",
            "searchTerm": tvdb_id,
            "mediaType": media_type,
            "importMethod": "auto",
            "originalKeyword": title
        }
        
        # 统一处理电影和电视剧
        return await handle_media_import_flow(update, context, import_params)
    else:
        await update.message.reply_text(f"❌ TVDB查询失败\n\n无法找到slug '{slug}' 对应的媒体信息，请检查链接是否正确。")
        return ConversationHandler.END

async def process_douban_input(update: Update, context: ContextTypes.DEFAULT_TYPE, input_info: dict):
    """处理豆瓣链接输入"""
    douban_id = input_info["douban_id"]
    # 豆瓣URL解析时没有返回media_type，默认设置为tv_series
    # media_type = input_info.get("media_type", "tv_series")
    
    # 第一步：立即显示检测结果
    # type_icon = "📺" if media_type == 'tv_series' else "🎬"
    await update.message.reply_text(f"检测到豆瓣ID: {douban_id}")
    
    # 第二步：显示正在查询的状态
    await update.message.reply_text("🔍 正在查询豆瓣API获取详细信息...")
    
    # 通过爬虫获取详细信息
    from utils.douban_scraper import get_douban_media_info
    douban_result = await get_douban_media_info(douban_id)
    logger.info(f"Douban result: {douban_result}")
    if douban_result and douban_result.get("success") and "title" in douban_result:
        title = douban_result["title"]
        media_type = douban_result["media_type"]
        type_icon = "📺" if media_type == 'tv_series' else "🎬"
        type_name = "电视剧" if media_type == 'tv_series' else "电影"
        
        # 根据媒体类型显示不同的提示信息
        if media_type == "movie":
            status_text = "🚀 开始自动导入..."
        else:
            status_text = "🔍 正在获取季度信息..."
        
        await update.message.reply_text(
            f"✅ **豆瓣查询成功**\n\n"
            f"🎬 标题: {title}\n"
            f"🆔 ID: `{douban_id}`\n"
            f"{type_icon} 类型: {type_name}\n\n"
            f"{status_text}",
            parse_mode="Markdown"
        )
        
        # 构建导入参数
        import_params = {
            "searchType": "douban",
            "searchTerm": douban_id,
            "mediaType": media_type,
            "importMethod": "auto",
            "originalKeyword": title
        }
        
        # 统一处理电影和电视剧
        return await handle_media_import_flow(update, context, import_params)
    else:
        await update.message.reply_text(f"❌ 豆瓣查询失败\n\n无法找到ID '{douban_id}' 对应的媒体信息，请检查链接是否正确。")
        return ConversationHandler.END

async def process_imdb_input(update: Update, context: ContextTypes.DEFAULT_TYPE, input_info: dict):
    """处理IMDB链接输入"""
    imdb_id = input_info["imdb_id"]
    media_type = input_info.get("media_type")  # 从URL ref参数获取的类型
    
    # 域名验证（仅对完整URL输入进行验证）
    logger.info(f"input_info: {input_info}")
    original_url = input_info.get("original_url")
    if original_url:
        # 只有当输入的是完整URL时才进行域名验证
        from utils.url_parser import is_imdb_url
        if not is_imdb_url(original_url):
            await update.message.reply_text(
                "❌ **域名验证失败**\n\n"
                "请确保输入的是有效的IMDB链接：\n"
                "• https://www.imdb.com/title/ttxxxxxxx/\n"
                "• https://m.imdb.com/title/ttxxxxxxx/",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
    
    # 判断输入类型并显示相应信息
    if original_url:
        # 输入的是完整URL链接
        await update.message.reply_text(f"🌟 检测到IMDB链接\n\n🆔 ID: {imdb_id}")
    else:
        # 输入的是纯ID（tt开头或纯数字）
        await update.message.reply_text(f"🌟 检测到IMDB ID\n\n🆔 ID: {imdb_id}")
    
    if media_type:
        # 如果URL中包含类型信息
        type_name = '电视剧/动漫' if media_type == 'tv' else '电影'
        await update.message.reply_text(f"🎭 类型: {type_name}")
        
        # 标准化媒体类型
        if media_type == 'tv':
            media_type = 'tv_series'
    else:
        # 需要通过爬虫获取类型
        await update.message.reply_text("🔍 正在获取IMDB媒体信息...")
        
        try:
            imdb_info = get_imdb_info(imdb_id)
            
            if imdb_info and imdb_info.get('success'):
                media_title = imdb_info.get('title', 'N/A')
                media_year = imdb_info.get('year', 'N/A')
                rating = imdb_info.get('rating', 'N/A')
                
                # 获取媒体类型
                media_type = imdb_info.get('media_type', 'movie')
                if media_type == 'tv_series':
                    type_name = '电视剧/动漫'
                else:
                    type_name = '电影'
                
                await update.message.reply_text(
                    f"✅ **IMDB信息获取成功**\n\n"
                    f"🎬 标题: {media_title}\n"
                    f"📅 年份: {media_year}\n"
                    f"🎭 类型: {type_name}\n"
                    f"⭐ 评分: {rating}\n\n",
                    parse_mode="Markdown"
                )
            else:
                error_msg = imdb_info.get('error', '未知错误') if imdb_info else '网络请求失败'
                logger.warning(f"IMDB信息解析失败，直接使用ID导入: {error_msg}")
                await update.message.reply_text(
                    f"⚠️ **IMDB信息解析失败，将直接使用ID导入**\n\n"
                    f"📋 IMDB ID: `{imdb_id}`\n"
                    f"🔄 跳过详细信息获取，直接进行导入...",
                    parse_mode="Markdown"
                )
                media_type = "tv_series"  # 默认为电视剧
        except Exception as e:
            logger.error(f"IMDB爬虫异常: imdb_id='{imdb_id}', error={str(e)}")
            logger.warning(f"IMDB信息解析异常，直接使用ID导入: {str(e)}")
            await update.message.reply_text(
                f"⚠️ **IMDB信息解析异常，将直接使用ID导入**\n\n"
                f"📋 IMDB ID: `{imdb_id}`\n"
                f"🔄 跳过详细信息获取，直接进行导入...",
                parse_mode="Markdown"
            )
            media_type = "tv_series"  # 默认为电视剧
    
    # 构建导入参数
    import_params = {
        "searchType": "imdb",
        "searchTerm": imdb_id,
        "mediaType": media_type,
        "importMethod": "auto",
        "originalKeyword": f"IMDB ID: {imdb_id}"
    }
    
    # 统一处理电影和电视剧
    return await handle_media_import_flow(update, context, import_params)

async def process_bgm_input(update: Update, context: ContextTypes.DEFAULT_TYPE, input_info: dict):
    """处理BGM链接输入"""
    bgm_id = input_info["bgm_id"]
    
    await update.message.reply_text(f"🌸 检测到 Bangumi 链接\n\n🆔 ID: {bgm_id}")
    await update.message.reply_text("🔍 正在获取BGM媒体信息...")
    
    try:
        bgm_info = get_bgm_info(bgm_id)
        
        if bgm_info and bgm_info.get('success'):
            media_title = bgm_info.get('title', 'N/A')
            media_year = bgm_info.get('year', 'N/A')
            rating = bgm_info.get('rating', 'N/A')
            
            await update.message.reply_text(
                f"✅ **BGM信息获取成功**\n\n"
                f"🎬 标题: {media_title}\n"
                f"📅 年份: {media_year}\n"
                f"📺 类型: 动漫\n"
                f"⭐ 评分: {rating}\n\n"
                f"🚀 开始自动导入...",
                parse_mode="Markdown"
            )
            
            # 构建导入参数
            import_params = {
                "searchType": "bangumi",
                "searchTerm": bgm_id,
                "mediaType": "tv_series",
                "importMethod": "auto",
                "originalKeyword": media_title
            }
        else:
            error_msg = bgm_info.get('error', '未知错误') if bgm_info else '网络请求失败'
            logger.warning(f"BGM信息解析失败，直接使用ID导入: {error_msg}")
            await update.message.reply_text(
                f"⚠️ **BGM信息解析失败，将直接使用ID导入**\n\n"
                f"📋 BGM ID: `{bgm_id}`\n"
                f"🔄 跳过详细信息获取，直接进行导入...",
                parse_mode="Markdown"
            )
            
            # 构建导入参数
            import_params = {
                "searchType": "bangumi",
                "searchTerm": bgm_id,
                "mediaType": "tv_series",
                "importMethod": "auto",
                "originalKeyword": f"BGM ID: {bgm_id}"
            }
    except Exception as e:
        logger.error(f"BGM爬虫异常: bgm_id='{bgm_id}', error={str(e)}")
        logger.warning(f"BGM信息解析异常，直接使用ID导入: {str(e)}")
        
        await update.message.reply_text(
            f"⚠️ **BGM信息解析异常，将直接使用ID导入**\n\n"
            f"📋 BGM ID: `{bgm_id}`\n"
            f"🔄 跳过详细信息获取，直接进行导入...",
            parse_mode="Markdown"
        )
        
        # 构建导入参数
        import_params = {
            "searchType": "bangumi",
            "searchTerm": bgm_id,
            "mediaType": "tv_series",
            "importMethod": "auto",
            "originalKeyword": f"BGM ID: {bgm_id}"
        }
    
    # 统一处理电影和电视剧
    return await handle_media_import_flow(update, context, import_params)

async def process_keyword_search(update: Update, context: ContextTypes.DEFAULT_TYPE, input_text: str):
    """处理关键词搜索"""
    keyword = input_text.strip()
    
    # 保存关键词到上下文
    context.user_data["import_auto_search_type"] = "keyword"
    context.user_data["import_auto_keyword"] = keyword
    
    # 检查TMDB配置
    from config import TMDB_ENABLED
    
    if TMDB_ENABLED:
        await update.message.reply_text(f"🔍 关键词搜索: {keyword}\n\n正在使用TMDB辅助搜索...")
        
        # 使用TMDB进行智能识别
        suggested_type = get_media_type_suggestion(keyword)
        tmdb_info = format_tmdb_results_info(keyword)
    else:
        await update.message.reply_text(f"🔍 关键词搜索: {keyword}\n\nℹ️ 未配置TMDB API Key，将跳过TMDB辅助搜索")
        suggested_type = None
        tmdb_info = None
    
    if suggested_type:
        # TMDB智能识别成功
        type_name = "📺 电视剧/动漫" if suggested_type == "tv_series" else "🎬 电影"
        
        await update.message.reply_text(
            f"🎯 **TMDB智能识别**\n\n{tmdb_info}\n\n✅ 自动识别类型：{type_name}",
            parse_mode="Markdown"
        )
        
        # 尝试获取TMDB ID进行精确搜索
        from utils.tmdb_api import search_tmdb_multi, get_tmdb_media_details
        search_result = search_tmdb_multi(keyword)
        best_match = search_result.get_best_match() if search_result else None
        tmdb_id = str(best_match.get('id')) if best_match else None
        
        # 如果是电视剧且有TMDB ID，预先获取季度信息
        if tmdb_id and suggested_type == "tv_series":
            try:
                media_details = get_tmdb_media_details(tmdb_id, "tv_series")
                if media_details and 'processed_seasons' in media_details:
                    logger.info(f"🎯 预先获取TMDB季度信息成功，共{len(media_details['processed_seasons'])}季")
            except Exception as e:
                logger.warning(f"预先获取TMDB季度信息失败: {e}")
        
        # 构建导入参数
        if tmdb_id and suggested_type == "tv_series":
            # 使用TMDB ID进行搜索
            import_params = {
                "searchType": "tmdb",
                "searchTerm": tmdb_id,
                "mediaType": suggested_type,
                "importMethod": "auto",
                "originalKeyword": keyword
            }
        else:
            # 使用关键词搜索
            import_params = {
                "searchType": "keyword",
                "searchTerm": keyword,
                "mediaType": suggested_type,
                "importMethod": "auto",
                "originalKeyword": keyword
            }
        
        # 统一处理电影和电视剧
        return await handle_media_import_flow(update, context, import_params)
    else:
        # 需要手动选择媒体类型
        message_text = f"🔍 **关键词搜索: {keyword}**\n\n"
        
        if tmdb_info is None:
            # 未配置TMDB
            message_text += "❓ 请手动选择媒体类型：\n\n"
        elif tmdb_info == "🔍 TMDB未找到相关结果":
            # TMDB未找到结果
            message_text += f"{tmdb_info}\n\n❓ 未找到TMDB数据，请手动选择媒体类型：\n\n"
        else:
            # TMDB找到多种类型
            try:
                await update.message.reply_text(
                    f"{message_text}{tmdb_info}\n\n🔍 **检测到多种媒体类型，正在为您启动搜索流程以获得更精确的结果...**",
                    parse_mode="Markdown"
                )
                
                # 启动搜索流程
                await process_search_media(update, keyword, context)
                return SEARCH_RESULTS
            except Exception as e:
                logger.error(f"启动搜索流程失败: {e}")
        
        # 显示媒体类型选择按钮
        keyboard = [
            [InlineKeyboardButton("📺 电视剧/动漫", callback_data=json.dumps({"action": "import_auto_media_type", "type": "tv_series"}, ensure_ascii=False))],
            [InlineKeyboardButton("🎬 电影", callback_data=json.dumps({"action": "import_auto_media_type", "type": "movie"}, ensure_ascii=False))]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        
        return 2  # 等待媒体类型选择

async def handle_media_import_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, import_params: dict):
    """统一处理媒体导入流程"""
    media_type = import_params["mediaType"]
    
    if media_type == "movie":
        # 电影：直接导入
        await call_import_auto_api(update, context, import_params)
        return ConversationHandler.END
    else:
        # 电视剧：显示导入方式选择
        context.user_data["import_auto_search_type"] = import_params["searchType"]
        context.user_data["import_auto_id"] = import_params["searchTerm"]
        context.user_data["import_auto_media_type"] = media_type
        
        return await show_import_options(update, context, import_params)

async def process_auto_input(update: Update, context: ContextTypes.DEFAULT_TYPE, input_text: str):
    """处理自动输入的文字或链接"""
    # 验证域名
    input_info = determine_input_type(input_text)
    
    if input_info["type"] == "invalid_domain":
        await update.message.reply_text(
            "❌ **域名验证失败**\n\n"
            "请确保输入的链接来自支持的平台：\n"
            "• TMDB、TVDB、IMDB、豆瓣、BGM等",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # 统一处理各种输入类型
    return await process_media_input_unified(update, context, input_info, input_text)


async def import_auto_season_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理季度选择"""
    query = update.callback_query
    logger.info(f"🎭 进入季度选择处理函数，回调数据: {query.data}")
    logger.info(f"🔍 用户数据上下文: {context.user_data}")
    
    try:
        await query.answer()
        logger.info(f"✅ 回调查询应答成功")
    except Exception as answer_error:
        logger.error(f"❌ 回调查询应答失败: {answer_error}")
    
    if query.data == "cancel":
        logger.info("❌ 用户取消导入操作")
        try:
            await query.edit_message_text("❌ 已取消导入操作")
            logger.info("✅ 取消消息发送成功")
        except Exception as cancel_error:
            logger.error(f"❌ 取消消息发送失败: {cancel_error}")
        return ConversationHandler.END
    
    if query.data.startswith("season_"):
        try:
            season_number = int(query.data.replace("season_", ""))
            logger.info(f"✅ 用户选择季度: {season_number}")
            context.user_data["selected_season"] = season_number
            
            # 获取导入参数
            import_params = context.user_data.get("import_auto_params", {})
            logger.info(f"📋 原始导入参数: {import_params}")
            
            # 检查是否需要进行识别词匹配
            original_keyword = import_params.get("originalKeyword")
            search_type = import_params.get("searchType")
            
            # 如果没有识别词匹配，显示默认的季度选择确认消息
            if not (original_keyword and search_type in ["keyword", "tmdb"] and convert_emby_series_name(original_keyword, season_number)):
                try:
                    await query.edit_message_text(
                        f"✅ **已选择第{season_number}季**\n\n"
                        f"🚀 开始导入选定季度的内容..."
                    )
                    logger.info(f"✅ 季度选择确认消息发送成功")
                except Exception as edit_error:
                    logger.error(f"❌ 季度选择确认消息发送失败: {edit_error}")
            
            if original_keyword and search_type in ["keyword", "tmdb"]:
                logger.info(f"🔍 开始检查识别词匹配，原始关键词: {original_keyword}，季度: {season_number}")
                
                converted_result = convert_emby_series_name(original_keyword, season_number)
                if converted_result:
                    logger.info(f"✅ 识别词匹配成功: {original_keyword} -> {converted_result}")
                    
                    # 提取搜索关键词
                    if isinstance(converted_result, dict) and 'series_name' in converted_result:
                        search_keyword = converted_result['series_name']
                        display_result = f"剧集名称: {converted_result['series_name']}, 季度: {converted_result.get('season_number', season_number)}"
                    else:
                        search_keyword = str(converted_result)
                        display_result = str(converted_result)
                    
                    try:
                        await query.edit_message_text(
                            f"✅ **已选择第{season_number}季**\n\n"
                            f"🎯 **识别词匹配成功**\n"
                            f"原始关键词: {original_keyword}\n"
                            f"转换关键词: {search_keyword}\n\n"
                            f"🔍 正在搜索转换后的关键词..."
                        )
                    except Exception as edit_error:
                        logger.error(f"❌ 识别词匹配消息发送失败: {edit_error}")
                    
                    # 使用转换后的关键词进行搜索
                    logger.info(f"🔍 使用转换后的关键词进行搜索: {search_keyword}")
                    
                    # 调用搜索API
                    api_result = call_danmaku_api(
                        method="GET",
                        endpoint="/search",
                        params={"keyword": search_keyword}
                    )
                    
                    # 处理搜索结果
                    if not api_result["success"]:
                        try:
                            await query.edit_message_text(f"❌ 搜索失败：{api_result['error']}")
                        except Exception as edit_error:
                            logger.error(f"❌ 搜索失败消息发送失败: {edit_error}")
                        return ConversationHandler.END
                    
                    search_data = api_result["data"]
                    search_id = search_data.get("searchId", "")
                    items = search_data.get("results", [])
                    
                    if not search_id:
                        try:
                            await query.edit_message_text("❌ 搜索结果缺少searchId，无法后续导入")
                        except Exception as edit_error:
                            logger.error(f"❌ 搜索ID缺失消息发送失败: {edit_error}")
                        return ConversationHandler.END
                    
                    if not items:
                         try:
                             await query.edit_message_text(f"❌ 未找到关键词「{search_keyword}」的媒体")
                         except Exception as edit_error:
                             logger.error(f"❌ 搜索无结果消息发送失败: {edit_error}")
                         return ConversationHandler.END
                    
                    # 保存搜索结果到上下文
                    context.user_data["search_id"] = search_id
                    context.user_data["search_results"] = items
                    
                    # 显示搜索结果
                    from callback.import_media import show_paged_results
                    
                    # 创建一个临时的update对象用于显示结果
                    class TempUpdate:
                        def __init__(self, query):
                            self.callback_query = query
                            self.message = query.message
                    
                    temp_update = TempUpdate(query)
                    await show_paged_results(temp_update, context, items, page=0, per_page=5)
                    
                    # 返回搜索结果状态
                    return SEARCH_RESULTS
                else:
                    logger.info(f"ℹ️ 未找到匹配的识别词配置: {original_keyword}")
            else:
                logger.debug(f"🔍 跳过识别词匹配检查 - 原始关键词: {original_keyword}, 搜索类型: {search_type}")
            
            import_params["season"] = season_number
            import_params["importMethod"] = "auto"  # 添加导入方式参数
            
            # 确保originalKeyword参数存在，用于识别词匹配
            if "originalKeyword" not in import_params:
                # 从上下文中获取原始关键词
                original_keyword_from_context = context.user_data.get("import_auto_keyword", "")
                if original_keyword_from_context:
                    import_params["originalKeyword"] = original_keyword_from_context
                else:
                    # 如果上下文中也没有，使用searchTerm作为备选
                    import_params["originalKeyword"] = import_params.get("searchTerm", "")
            
            logger.info(f"📋 添加季度后的导入参数: {import_params}")
            
            # 调用导入API
            logger.info(f"🚀 准备调用导入API")
            await call_import_auto_api(update, context, import_params)
            logger.info(f"✅ 导入API调用完成")
            return ConversationHandler.END
            
        except ValueError as value_error:
            logger.error(f"❌ 季度数字解析失败: {value_error}")
            try:
                await query.edit_message_text("❌ 无效的季度选择")
            except Exception as error_msg_error:
                logger.error(f"❌ 错误消息发送失败: {error_msg_error}")
            return ConversationHandler.END
        except Exception as season_error:
            logger.error(f"❌ 季度选择处理异常: {season_error}")
            try:
                await query.edit_message_text(f"❌ 处理季度选择时出错: {str(season_error)}")
            except Exception as error_msg_error:
                logger.error(f"❌ 异常消息发送失败: {error_msg_error}")
            return ConversationHandler.END
    
    logger.warning(f"⚠️ 未识别的回调数据: {query.data}")
    try:
        await query.edit_message_text("❌ 无效的选择")
    except Exception as invalid_error:
        logger.error(f"❌ 无效选择消息发送失败: {invalid_error}")
    return ConversationHandler.END


async def import_auto_keyword_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收用户输入的关键词"""
    keyword = update.message.text.strip()
    if not keyword:
        await update.message.reply_text("❌ 关键词不能为空，请重新输入：")
        return IMPORT_AUTO_KEYWORD_INPUT
    
    # 保存关键词到上下文
    context.user_data["import_auto_keyword"] = keyword
    
    # 检查TMDB是否启用
    from config import TMDB_ENABLED
    
    if TMDB_ENABLED:
        await update.message.reply_text(f"🔍 关键词搜索: {keyword}\n\n正在使用TMDB辅助搜索...")
        
        # 尝试TMDB辅助搜索
        suggested_type = get_media_type_suggestion(keyword)
        tmdb_info = format_tmdb_results_info(keyword)
    else:
        await update.message.reply_text(f"🔍 关键词搜索: {keyword}\n\nℹ️ 未配置TMDB API Key，将跳过TMDB辅助搜索")
        suggested_type = None
        tmdb_info = None
    
    if suggested_type:
        # TMDB建议了明确的类型
        type_name = "电视剧/动漫" if suggested_type == "tv_series" else "电影"
        
        # 根据类型显示对应图标
        if suggested_type == "movie":
            type_icon = "🎬"
        else:
            type_icon = "📺"
        
        await update.message.reply_text(
            f"🎯 **TMDB智能识别**\n\n{tmdb_info}\n\n✅ 自动识别类型：{type_icon} {type_name}",
            parse_mode="Markdown"
        )
        
        # 获取最佳匹配的TMDB ID和详细信息
        from utils.tmdb_api import search_tmdb_multi, get_tmdb_media_details
        search_result = search_tmdb_multi(keyword)
        best_match = search_result.get_best_match() if search_result else None
        tmdb_id = str(best_match.get('id')) if best_match else None
        
        # 对于电视剧，预先获取详细信息（包含季度信息）以优化后续处理
        if tmdb_id and suggested_type == "tv_series":
            # 预先获取详细信息，这样后续的季度选择就不需要额外API调用
            media_details = get_tmdb_media_details(tmdb_id, "tv_series")
            if media_details and 'processed_seasons' in media_details:
                logger.info(f"🎯 预先获取TMDB季度信息成功，共{len(media_details['processed_seasons'])}季")
        
        # 构建导入参数
        if tmdb_id and suggested_type == "tv_series":
            # 电视剧且有TMDB ID：使用TMDB搜索
            import_params = {
                "searchType": "tmdb",
                "searchTerm": tmdb_id,
                "mediaType": suggested_type,
                "importMethod": "auto",
                "originalKeyword": keyword  # 保存原始关键词用于识别词匹配
            }
        else:
            # 电影或无TMDB ID：使用关键词搜索
            import_params = {
                "searchType": "keyword",
                "searchTerm": keyword,
                "mediaType": suggested_type,
                "importMethod": "auto",
                "originalKeyword": keyword  # 保存原始关键词用于识别词匹配
            }
        
        # 根据类型决定处理方式
        if suggested_type == "movie":
            # 电影类型：直接导入
            await update.message.reply_text("🚀 开始自动导入...")
            await call_import_auto_api(update, context, import_params)
            return ConversationHandler.END
        else:
            # 电视剧类型：进入季度选择流程
            return await show_import_options(update, context, import_params)
    else:
        # TMDB无法确定类型或未启用，显示手动选择
        message_text = f"🔍 **关键词搜索: {keyword}**\n\n"
        
        if tmdb_info is None:
            # TMDB未启用
            message_text += "❓ 请手动选择媒体类型：\n\n"
        elif tmdb_info != "🔍 TMDB未找到相关结果":
            # TMDB启用但类型混合
            message_text += f"{tmdb_info}\n\n⚠️ 类型混合，请手动选择：\n\n"
        else:
            # TMDB启用但未找到结果
            message_text += f"{tmdb_info}\n\n❓ 未找到TMDB数据，请手动选择媒体类型：\n\n"
        
        keyboard = [
            [InlineKeyboardButton("📺 电视剧/动漫", callback_data=json.dumps({"action": "import_auto_media_type", "type": "tv_series"}, ensure_ascii=False))],
            [InlineKeyboardButton("🎬 电影", callback_data=json.dumps({"action": "import_auto_media_type", "type": "movie"}, ensure_ascii=False))]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return 2  # 等待媒体类型选择


def validate_platform_match(user_input: str, selected_platform: str) -> tuple[bool, str]:
    """验证用户输入的链接是否与选择的平台匹配
    
    Args:
        user_input: 用户输入的文本
        selected_platform: 用户选择的平台 (tmdb, tvdb, bangumi, douban, imdb)
        
    Returns:
        tuple: (是否匹配, 错误消息)
    """
    result = determine_input_type(user_input)
    input_type = result.get('type')
    
    # 如果输入的是纯ID或关键词，则不需要验证
    if input_type in ['keyword', 'tt_id'] or not input_type.endswith('_url'):
        return True, ""
    
    # 平台映射
    platform_mapping = {
        'tmdb': 'tmdb_url',
        'tvdb': 'tvdb_url', 
        'bangumi': 'bgm_url',
        'douban': 'douban_url',
        'imdb': 'imdb_url'
    }
    
    expected_type = platform_mapping.get(selected_platform)
    if not expected_type:
        return True, ""  # 未知平台，跳过验证
    
    if input_type != expected_type:
        # 构建错误消息
        platform_names = {
            'tmdb': 'TMDB',
            'tvdb': 'TVDB', 
            'bangumi': 'BGM/Bangumi',
            'douban': '豆瓣',
            'imdb': 'IMDB'
        }
        
        detected_platform = {
            'tmdb_url': 'TMDB',
            'tvdb_url': 'TVDB',
            'bgm_url': 'BGM/Bangumi', 
            'douban_url': '豆瓣',
            'imdb_url': 'IMDB'
        }.get(input_type, '未知')
        
        selected_name = platform_names.get(selected_platform, selected_platform)
        
        error_msg = (
            f"❌ **平台不匹配**\n\n"
            f"🎯 您选择的平台: **{selected_name}**\n"
            f"🔍 检测到的平台: **{detected_platform}**\n\n"
            f"💡 **解决方案:**\n"
            f"• 请输入正确的 {selected_name} 链接\n"
            f"• 或者输入纯 ID 进行搜索\n"
            f"• 或者重新选择正确的平台"
        )
        return False, error_msg
    
    return True, ""


async def show_season_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, seasons: list, media_info: dict):
    """显示季度选择界面
    
    Args:
        update: Telegram更新对象
        context: 上下文对象
        seasons: 季度信息列表
        media_info: 媒体基本信息
    """
    if not seasons or len(seasons) == 0:
        # 没有找到季度信息，使用默认导入（电视剧设置season=1）
        await send_message_safe(update, context,
            f"⚠️ **未找到季度信息**\n\n"
            f"🎬 标题: {media_info.get('title', 'N/A')}\n"
            f"📅 年份: {media_info.get('year', 'N/A')}\n\n"
            f"✅ 使用默认方式导入（第1季）..."
        )
        
        import_params = context.user_data.get("import_auto_params", {})
        import_params["season"] = 1  # 电视剧默认导入第1季
        await call_import_auto_api(update, context, import_params)
        return ConversationHandler.END
        
    elif len(seasons) == 1:
        # 只有一季，直接导入season=1
        season = seasons[0]
        await send_message_safe(update, context,
            f"📺 **检测到单季电视剧**\n\n"
            f"🎬 标题: {media_info.get('title', 'N/A')}\n"
            f"📅 年份: {media_info.get('year', 'N/A')}\n"
            f"🎭 季度: {season['name']}\n\n"
            f"✅ 自动导入单季内容..."
        )
        
        # 电视剧单季直接设置season=1
        context.user_data["selected_season"] = 1
        import_params = context.user_data.get("import_auto_params", {})
        import_params["season"] = 1
        
        await call_import_auto_api(update, context, import_params)
        return ConversationHandler.END
    
    elif len(seasons) > 1:
        # 多季，显示选择界面
        title = media_info.get('title', 'N/A')
        year = media_info.get('year', 'N/A')
        
        message_text = (
            f"🎭 共找到 {len(seasons)} 季\n\n"
            f"请选择要导入的季度:"
        )
        
        # 创建季度选择按钮
        keyboard = []
        for season in seasons:
            season_num = season['season_number']
            season_name = season['name']
            episode_info = f" ({season['episode_count']}集)" if season['episode_count'] > 0 else ""
            
            button_text = f"{season_name}{episode_info}"
            callback_data = f"season_{season_num}"
            
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        # 添加取消按钮
        keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await send_message_safe(update, context, message_text, reply_markup=reply_markup)
        
        return IMPORT_AUTO_SEASON_SELECTION


async def import_auto_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收用户输入的平台ID或链接"""
    user_input = update.message.text.strip()
    if not user_input:
        await update.message.reply_text("❌ 输入不能为空，请重新输入：")
        return IMPORT_AUTO_ID_INPUT
    
    search_type = context.user_data.get("import_auto_search_type", "tmdb")
    
    # 验证平台匹配
    is_valid, error_msg = validate_platform_match(user_input, search_type)
    if not is_valid:
        await update.message.reply_text(error_msg)
        return IMPORT_AUTO_ID_INPUT  # 继续等待正确的输入
    
    # 验证域名
    input_info = determine_input_type(user_input)
    
    if input_info["type"] == "invalid_domain":
        await update.message.reply_text(
            "❌ **域名验证失败**\n\n"
            "请确保输入的链接来自支持的平台：\n"
            "• TMDB、TVDB、IMDB、豆瓣、BGM等",
            parse_mode="Markdown"
        )
        return IMPORT_AUTO_ID_INPUT

    # 统一处理各种输入类型
    return await process_media_input_unified(update, context, input_info, user_input)





# 已移除import_auto_season_input和import_auto_episode_input函数，因为不再需要分季导入和分集导入功能


# 所有对话状态常量已移至 utils/conversation_states.py
# 当前文件已从该模块导入所需的所有常量


async def send_message_safe(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """安全发送消息，处理回调更新时 update.message 为 None 的情况"""
    if update.message:
        return await update.message.reply_text(text, **kwargs)
    else:
        return await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            **kwargs
        )

async def show_import_options(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict):
    """显示导入选项，对于电视剧检查季度信息"""
    # 保存参数到上下文
    context.user_data["import_auto_params"] = params
    
    # 检查是否为电视剧类型
    if params.get("mediaType") == "tv_series":
        # 电视剧类型：检查季度信息
        search_type = params.get("searchType")
        search_term = params.get("searchTerm")
        
        await send_message_safe(update, context, "🔍 正在获取季度信息...")
        
        # 根据平台获取季度信息
        seasons = None
        media_info = {}
        
        try:
            if search_type == "tmdb":
                # 优化：直接从get_tmdb_media_details获取详情和季度信息
                from utils.tmdb_api import get_tmdb_media_details
                media_details = get_tmdb_media_details(search_term, "tv_series")
                
                if media_details:
                    # 获取季度信息（优先使用已处理的季度信息）
                    if 'processed_seasons' in media_details:
                        seasons = media_details['processed_seasons']
                    else:
                        # 如果没有预处理，调用get_tmdb_tv_seasons
                        seasons = get_tmdb_tv_seasons(search_term)
                    
                    # 提取媒体基本信息
                    title = media_details.get('title') or media_details.get('name', f'TMDB ID: {search_term}')
                    first_air_date = media_details.get('first_air_date', '')
                    year = first_air_date[:4] if first_air_date else 'N/A'
                    media_info = {'title': title, 'year': year}
                else:
                    # 如果获取详情失败，回退到原来的方式
                    seasons = get_tmdb_tv_seasons(search_term)
                    media_info = {'title': f'TMDB ID: {search_term}', 'year': 'N/A'}
                    
            elif search_type == "tvdb":
                seasons = get_tvdb_tv_seasons(search_term)
                media_info = {'title': f'TVDB ID: {search_term}', 'year': 'N/A'}
                
            elif search_type == "imdb":
                # 从IMDB爬虫获取信息（包含季度信息）
                imdb_info = get_imdb_info(search_term)
                if imdb_info and imdb_info.get('success'):
                    seasons = imdb_info.get('seasons', [])
                    media_info = {
                        'title': imdb_info.get('title', f'IMDB ID: {search_term}'),
                        'year': imdb_info.get('year', 'N/A')
                    }
                else:
                    media_info = {'title': f'IMDB ID: {search_term}', 'year': 'N/A'}
                    
            elif search_type == "douban":
                # 从豆瓣爬虫获取信息（包含从标题解析的季度信息）
                from utils.douban_scraper import get_douban_info
                douban_info = get_douban_info(search_term)
                if douban_info:
                    # 如果豆瓣爬虫解析出了季度信息，直接使用
                    season_number = douban_info.get('season')
                    if season_number:
                        # 直接导入指定季度
                        params["season"] = season_number
                        params["importMethod"] = "auto"
                        
                        await send_message_safe(update, context,
                            f"✅ **从标题解析出季度信息**\n\n"
                            f"🎬 标题: {douban_info.get('title', 'N/A')}\n"
                            f"📅 年份: {douban_info.get('year', 'N/A')}\n"
                            f"🎭 季度: 第{season_number}季\n\n"
                            f"🚀 开始导入第{season_number}季内容..."
                        )
                        
                        await call_import_auto_api(update, context, params)
                        return ConversationHandler.END
                    else:
                        # 没有解析出季度信息，默认设置为第1季
                        params["season"] = 1
                        await send_message_safe(update, context,
                            f"✅ **未从标题解析出季度信息，默认导入第1季**\n\n"
                            f"🎬 标题: {douban_info.get('title', 'N/A')}\n"
                            f"📅 年份: {douban_info.get('year', 'N/A')}\n"
                            f"🎭 季度: 第1季（默认）\n\n"
                            f"🚀 开始导入第1季内容..."
                        )
                        
                        await call_import_auto_api(update, context, params)
                        return ConversationHandler.END
                else:
                    seasons = None
                    media_info = {'title': f'豆瓣ID: {search_term}', 'year': 'N/A'}
                    
            elif search_type == "bangumi":
                # 从BGM爬虫获取信息（包含从标题解析的季度信息）
                bgm_info = get_bgm_info(search_term)
                if bgm_info and bgm_info.get('success'):
                    # 如果BGM爬虫解析出了季度信息，直接使用
                    season_number = bgm_info.get('season')
                    if season_number:
                        # 直接导入指定季度
                        params["season"] = season_number
                        params["importMethod"] = "auto"
                        
                        await send_message_safe(update, context,
                            f"✅ **从标题解析出季度信息**\n\n"
                            f"🎬 标题: {bgm_info.get('title', 'N/A')}\n"
                            f"📅 年份: {bgm_info.get('year', 'N/A')}\n"
                            f"🎭 季度: 第{season_number}季\n\n"
                            f"🚀 开始导入第{season_number}季内容..."
                        )
                        
                        await call_import_auto_api(update, context, params)
                        return ConversationHandler.END
                    else:
                        # 没有解析出季度信息，默认设置为第1季
                        params["season"] = 1
                        await send_message_safe(update, context,
                            f"✅ **未从标题解析出季度信息，默认导入第1季**\n\n"
                            f"🎬 标题: {bgm_info.get('title', 'N/A')}\n"
                            f"📅 年份: {bgm_info.get('year', 'N/A')}\n"
                            f"🎭 季度: 第1季（默认）\n\n"
                            f"🚀 开始导入第1季内容..."
                        )
                        
                        await call_import_auto_api(update, context, params)
                        return ConversationHandler.END
                else:
                    seasons = None
                    media_info = {'title': f'BGM ID: {search_term}', 'year': 'N/A'}
                    
        except Exception as e:
            logger.warning(f"获取季度信息失败: {e}")
            seasons = None
            
        # 调用季度选择函数
        return await show_season_selection(update, context, seasons, media_info)
    else:
        # 电影类型：直接执行自动导入，不需要season参数
        params["importMethod"] = "auto"
        # 确保电影类型不包含season参数
        if "season" in params:
            del params["season"]
        await call_import_auto_api(update, context, params)
        return ConversationHandler.END


# ===== 处理器创建函数 =====

def create_search_handler():
    """创建搜索媒体对话处理器"""
    return ConversationHandler(
        entry_points=[CommandHandler("search", wrap_conversation_entry_point(search_command))],
        states={
            SEARCH_MEDIA: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, 
                    wrap_with_session_management(search_media_input)
                )
            ],
            SEARCH_RESULTS: [
                # 在搜索结果状态下，用户可以点击按钮或取消
                # 按钮点击由独立的CallbackQueryHandler处理
                CommandHandler("cancel", wrap_with_session_management(cancel_command))
            ],
        },
        fallbacks=get_global_fallbacks(),
        per_chat=True,
        per_user=True,
        allow_reentry=True,
        persistent=False
    )


def create_import_auto_handler():
    """创建自动导入媒体对话处理器"""
    return ConversationHandler(
        entry_points=[CommandHandler("auto", wrap_conversation_entry_point(auto_command))],
        states={
            IMPORT_AUTO_SEARCH_TYPE: [CallbackQueryHandler(  
                wrap_with_session_management(handle_search_type_callback)
            )],
            IMPORT_AUTO_KEYWORD_INPUT: [
                MessageHandler(  
                    filters.TEXT & ~filters.COMMAND,
                    wrap_with_session_management(import_auto_keyword_input)
                ),
                CallbackQueryHandler(wrap_with_session_management(handle_media_type_callback)),
                CallbackQueryHandler(  # Handle all import_auto related callbacks
                    wrap_with_session_management(handle_import_auto_callback),
                    pattern=r'{"action": "import_auto_.*"}'
                )
            ],
            IMPORT_AUTO_ID_INPUT: [
                MessageHandler(  
                    filters.TEXT & ~filters.COMMAND,
                    wrap_with_session_management(import_auto_id_input)
                ),
                CallbackQueryHandler(  # Handle all import_auto related callbacks
                    wrap_with_session_management(handle_import_auto_callback),
                    pattern=r'{"action": "import_auto_.*"}'
                )
            ],
            IMPORT_AUTO_SEASON_SELECTION: [  
                CallbackQueryHandler(
                    wrap_with_session_management(handle_import_auto_callback),
                    pattern=r'(season_\d+|cancel|{"action": "import_auto_.*"})'
                )
            ],
        },
        fallbacks=get_global_fallbacks(),
        allow_reentry=True,  # 允许重新进入对话
        # 使用默认的 per_* 设置以避免混合处理器类型的警告
        per_chat=True,       # 每个聊天独立跟踪对话状态
        per_user=True,       # 每个用户独立跟踪对话状态
    )


def create_episode_input_handler():
    """创建集数输入对话处理器"""
    
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(
            wrap_with_session_management(handle_get_episode_callback),
            pattern=r'{"(action|a)": "(start_input_range|get_episodes|get_media_episode|switch_episode_page)".*}'
        )],  # 通过"输入集数区间"回调按钮、"获取分集"按钮或分页按钮触发
        states={
            INPUT_EPISODE_RANGE: [
                MessageHandler(  
                    filters.TEXT & ~filters.COMMAND,
                    wrap_with_session_management(handle_episode_range_input)
                ),
                CallbackQueryHandler(  # 处理分页按钮回调
                    wrap_with_session_management(handle_get_episode_callback),
                    pattern=r'^.*"switch_episode_page".*$'
                )
            ],
        },
        fallbacks=get_global_fallbacks(),
        # 使用默认的 per_* 设置以避免混合处理器类型的警告
        per_chat=True,   # 每个聊天独立跟踪对话状态
        per_user=True,   # 每个用户独立跟踪对话状态
        allow_reentry=True,
        persistent=False
    )


async def call_import_auto_api(update: Update, context: ContextTypes.DEFAULT_TYPE, params: dict):
    """调用/import/auto API"""
    # 判断是消息还是回调查询
    if update.callback_query:
        chat_id = update.callback_query.message.chat_id
        send_message = lambda text: context.bot.send_message(chat_id=chat_id, text=text)
        send_message_with_markup = lambda text, markup: context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    else:
        send_message = update.message.reply_text
        send_message_with_markup = lambda text, markup: update.message.reply_text(text, reply_markup=markup)
    
    # 移除中间状态提示，直接调用API
    logger.info(f"调用/import/auto API，参数: {params}")
    
    # 调用API
    api_result = call_danmaku_api(
        method="POST",
        endpoint="/import/auto",
        params=params
    )
    
    # 处理API响应
    if api_result["success"]:
        success_message = f"✅ 导入成功！"
        
        # 直接显示成功消息，不提供继续导入按钮
        await send_message(success_message)
    else:
        await send_message(f"❌ 导入失败：{api_result['error']}")

    