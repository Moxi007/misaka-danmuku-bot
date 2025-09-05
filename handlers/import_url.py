import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from utils.api import call_danmaku_api
from utils.permission import check_user_permission

logger = logging.getLogger(__name__)

# 对话状态
URL_INPUT, KEYWORD_INPUT, ANIME_SELECT, SOURCE_SELECT, EPISODE_INPUT = range(5)

async def check_url_accessibility(url: str) -> tuple[bool, str, str]:
    """检查URL是否可访问并解析标题
    
    Returns:
        tuple[bool, str, str]: (是否可访问, 错误信息或状态描述, 页面标题)
    """
    try:
        # 发送HEAD请求检查URL可访问性
        response = requests.head(url, timeout=10, allow_redirects=True)
        
        if response.status_code == 200:
            # HEAD请求成功，尝试获取页面内容解析标题（失败不影响主流程）
            title = ""
            try:
                content_response = requests.get(url, timeout=15, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                if content_response.status_code == 200:
                    title = extract_title_from_html(content_response.text)
            except Exception:
                # 标题解析失败，但不影响URL可访问性判断
                pass
            return True, "URL可访问", title
                
        elif response.status_code == 405:  # Method Not Allowed，尝试GET请求
            response = requests.get(url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            if response.status_code == 200:
                title = ""
                try:
                    title = extract_title_from_html(response.text)
                except Exception:
                    # 标题解析失败，但不影响URL可访问性判断
                    pass
                return True, "URL可访问", title
            else:
                return False, f"HTTP {response.status_code}: {response.reason}", ""
        else:
            return False, f"HTTP {response.status_code}: {response.reason}", ""
            
    except requests.exceptions.Timeout:
        return False, "请求超时，URL可能无法访问", ""
    except requests.exceptions.ConnectionError:
        return False, "连接失败，URL无法访问", ""
    except requests.exceptions.InvalidURL:
        return False, "无效的URL格式", ""
    except requests.exceptions.TooManyRedirects:
        return False, "重定向次数过多", ""
    except Exception as e:
        return False, f"检查失败: {str(e)[:50]}", ""

def extract_title_from_html(html_content: str) -> str:
    """从HTML内容中提取标题
    
    Args:
        html_content: HTML页面内容
        
    Returns:
        str: 页面标题，如果无法解析则返回空字符串
    """
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        title_tag = soup.find('title')
        if title_tag and title_tag.string:
            # 清理标题，移除多余的空白字符
            title = title_tag.string.strip()
            
            # 精准提取标题，去除常见的网站后缀和冗余信息
            title = clean_page_title(title)
            
            # 限制标题长度，避免过长
            if len(title) > 50:
                title = title[:47] + "..."
            return title
        return ""
    except Exception:
        return ""

def clean_page_title(title: str) -> str:
    """清理页面标题，去除网站名称和冗余信息
    
    Args:
        title: 原始标题
        
    Returns:
        str: 清理后的标题
    """
    # 常见的分隔符和网站后缀模式
    separators = ['_', '-', '|', '–', '—', '•']
    
    # 常见的视频网站关键词（用于识别和移除）
    video_site_keywords = [
        '腾讯视频', '爱奇艺', '优酷', '哔哩哔哩', 'bilibili', 'YouTube', 'Netflix',
        '在线观看', '高清完整版', '视频在线观看', '免费观看', '电影', '电视剧',
        '综艺', '动漫', '纪录片', '热映中', '正在热播', '全集'
    ]
    
    # 移除常见的冗余后缀
    redundant_suffixes = [
        '在线观看', '高清完整版视频在线观看', '电影高清完整版视频在线观看',
        '免费在线观看', '全集在线观看', '热映中', '正在热播'
    ]
    
    cleaned_title = title
    
    # 1. 移除冗余后缀
    for suffix in redundant_suffixes:
        if cleaned_title.endswith(suffix):
            cleaned_title = cleaned_title[:-len(suffix)].strip()
    
    # 2. 按分隔符分割，保留最有价值的部分
    for sep in separators:
        if sep in cleaned_title:
            parts = cleaned_title.split(sep)
            # 找到最长且不包含网站关键词的部分
            best_part = ""
            for part in parts:
                part = part.strip()
                # 跳过包含网站关键词的部分
                if any(keyword in part for keyword in video_site_keywords):
                    continue
                # 选择最长的有效部分
                if len(part) > len(best_part) and len(part) > 3:
                    best_part = part
            
            if best_part:
                cleaned_title = best_part
                break
    
    # 3. 提取《》或""中的内容（通常是作品名称）
    import re
    # 匹配《》中的内容
    book_title_match = re.search(r'《([^》]+)》', cleaned_title)
    if book_title_match:
        return book_title_match.group(1).strip()
    
    # 匹配""中的内容
    quote_title_match = re.search(r'"([^"]+)"', cleaned_title)
    if quote_title_match:
        return quote_title_match.group(1).strip()
    
    # 匹配''中的内容
    single_quote_match = re.search(r"'([^']+)'", cleaned_title)
    if single_quote_match:
        return single_quote_match.group(1).strip()
    
    return cleaned_title.strip()

# 重试命令处理器
async def retry_current_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重试当前步骤"""
    current_state = context.user_data.get('current_state')
    
    if current_state == URL_INPUT:
        await update.message.reply_text(
            "🔗 URL导入功能\n\n"
            "请发送要导入的视频URL："
        )
        return URL_INPUT
    elif current_state == KEYWORD_INPUT:
        await update.message.reply_text(
            "请输入关键词来搜索影视库："
        )
        return KEYWORD_INPUT
    elif current_state == ANIME_SELECT:
        matches = context.user_data.get('anime_matches', [])
        if matches:
            return await show_video_selection(update, context, matches)
        else:
            await update.message.reply_text(
                "❌ 没有找到之前的搜索结果，请重新输入关键词："
            )
            return KEYWORD_INPUT
    elif current_state == SOURCE_SELECT:
        anime = context.user_data.get('selected_anime')
        if anime:
            return await show_video_sources(update, context, anime)
        else:
            await update.message.reply_text(
                "❌ 没有找到选中的影视，请重新选择："
            )
            matches = context.user_data.get('anime_matches', [])
            if matches:
                return await show_video_selection(update, context, matches)
            else:
                return KEYWORD_INPUT
    elif current_state == EPISODE_INPUT:
        anime = context.user_data.get('selected_anime')
        source = context.user_data.get('selected_source')
        if anime and source:
            return await request_episode_input(update, context, anime, source)
        else:
            await update.message.reply_text(
                "❌ 缺少必要信息，请重新选择源："
            )
            if anime:
                return await show_video_sources(update, context, anime)
            else:
                return KEYWORD_INPUT
    else:
        # 默认回到开始
        await update.message.reply_text(
            "🔗 URL导入功能\n\n"
            "请发送要导入的视频URL："
        )
        return URL_INPUT

# 库缓存
library_cache = {
    'data': None,
    'timestamp': 0,
    'ttl': 3600  # 1小时缓存
}

async def get_library_data():
    """获取库数据，带缓存机制"""
    import time
    current_time = time.time()
    
    # 检查缓存是否有效
    if (library_cache['data'] is not None and 
        current_time - library_cache['timestamp'] < library_cache['ttl']):
        return library_cache['data']
    
    # 缓存过期或不存在，重新获取
    try:
        response = call_danmaku_api('GET', '/library')
        if response and 'success' in response and response['success']:
            library_cache['data'] = response.get('data', [])
            library_cache['timestamp'] = current_time
            logger.info(f"库数据已缓存，共 {len(library_cache['data'])} 条记录")
            return library_cache['data']
        else:
            logger.error(f"获取库数据失败: {response}")
            return []
    except Exception as e:
        logger.error(f"获取库数据异常: {e}")
        return []

async def init_library_cache():
    """初始化库缓存，在Bot启动时调用"""
    logger.info("🔄 正在初始化影视库缓存...")
    data = await get_library_data()
    if data:
        logger.info(f"✅ 影视库缓存初始化成功，共加载 {len(data)} 条记录")
    else:
        logger.warning("⚠️ 影视库缓存初始化失败，将在首次使用时重试")
    return data

def search_video_by_keyword(library_data, keyword):
    """根据关键词搜索影视"""
    keyword = keyword.lower().strip()
    matches = []
    
    for anime in library_data:
        title = anime.get('title', '').lower()
        if keyword in title:
            matches.append(anime)
    
    return matches

@check_user_permission
async def import_url_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始URL导入流程"""
    # 清理之前的数据并设置当前状态
    context.user_data.clear()
    context.user_data['current_state'] = URL_INPUT
    
    # 检查是否直接在命令中提供了URL参数
    command_args = context.args
    if command_args:
        # 获取URL参数（可能包含多个部分，需要重新组合）
        url = ' '.join(command_args).strip()
        
        # 简单的URL验证
        if url.startswith('http://') or url.startswith('https://'):
            # 检查URL可访问性并解析标题
            await update.message.reply_text("🔍 正在检查URL可访问性并解析页面标题...")
            
            is_accessible, status_msg, page_title = await check_url_accessibility(url)
            
            if is_accessible:
                # URL可访问，继续流程
                context.user_data['import_url'] = url
                context.user_data['page_title'] = page_title
                context.user_data['current_state'] = KEYWORD_INPUT
                
                title_info = f"\n📄 页面标题: {page_title}" if page_title else ""
                await update.message.reply_text(
                    f"✅ URL验证成功: {url}{title_info}\n\n"
                    "请输入关键词来搜索影视库：\n\n"
                    "💡 发送 /retry 重新执行当前步骤"
                )
                return KEYWORD_INPUT
            else:
                # URL不可访问
                await update.message.reply_text(
                    f"❌ URL无法访问: {url}\n\n"
                    f"错误信息: {status_msg}\n\n"
                    "请检查URL是否正确或稍后重试："
                )
                return URL_INPUT
        else:
            await update.message.reply_text(
                f"❌ 无效的URL格式: {url}\n\n"
                "请使用正确的格式：/url https://example.com/video\n\n"
                "或者直接发送URL："
            )
    
    # 没有提供URL参数或URL无效，进入正常流程
    await update.message.reply_text(
        "🔗 URL导入功能\n\n"
        "请发送要导入的视频URL：\n\n"
        "💡 提示：\n"
        "• 可以直接使用：/url https://example.com/video\n"
        "• 或者在任何步骤中发送 /retry 重新执行当前步骤"
    )
    return URL_INPUT

async def handle_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理URL输入"""
    url = update.message.text.strip()
    
    # 简单的URL验证
    if not (url.startswith('http://') or url.startswith('https://')):
        await update.message.reply_text(
            "❌ 请输入有效的URL（以http://或https://开头）\n\n"
            "💡 发送 /retry 重新输入URL"
        )
        return URL_INPUT
    
    # 检查URL可访问性并解析标题
    await update.message.reply_text("🔍 正在检查URL可访问性并解析页面标题...")
    
    is_accessible, status_msg, page_title = await check_url_accessibility(url)
    
    if is_accessible:
        # URL可访问，继续流程
        context.user_data['import_url'] = url
        context.user_data['page_title'] = page_title
        context.user_data['current_state'] = KEYWORD_INPUT
        
        title_info = f"\n📄 页面标题: {page_title}" if page_title else ""
        await update.message.reply_text(
            f"✅ URL验证成功: {url}{title_info}\n\n"
            "请输入关键词来搜索影视库：\n\n"
            "💡 发送 /retry 重新执行当前步骤"
        )
        return KEYWORD_INPUT
    else:
        # URL不可访问
        await update.message.reply_text(
            f"❌ URL无法访问: {url}\n\n"
            f"错误信息: {status_msg}\n\n"
            "请检查URL是否正确或稍后重试：\n\n"
            "💡 发送 /retry 重新输入URL"
        )
        return URL_INPUT

async def handle_keyword_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理关键词输入并搜索影视"""
    keyword = update.message.text.strip()
    
    if not keyword:
        await update.message.reply_text(
            "❌ 请输入有效的关键词\n\n"
            "💡 发送 /retry 重新执行当前步骤"
        )
        return KEYWORD_INPUT
    
    # 获取库数据
    library_data = await get_library_data()
    if not library_data:
        await update.message.reply_text(
            "❌ 无法获取影视库数据，请稍后重试\n\n"
            "💡 发送 /retry 重新执行当前步骤"
        )
        return KEYWORD_INPUT
    
    # 搜索匹配的影视
    matches = search_video_by_keyword(library_data, keyword)
    
    if not matches:
        await update.message.reply_text(
            f"❌ 未找到包含关键词 '{keyword}' 的影视\n\n"
            "请重新输入关键词：\n\n"
            "💡 发送 /retry 重新执行当前步骤"
        )
        return KEYWORD_INPUT
    
    # 保存搜索结果到上下文
    context.user_data['anime_matches'] = matches
    
    if len(matches) == 1:
        # 只有一个匹配结果，直接进入源选择
        video = matches[0]
        context.user_data['selected_anime'] = video
        context.user_data['current_state'] = SOURCE_SELECT
        return await show_video_sources(update, context, video)
    else:
        # 多个匹配结果，让用户选择
        context.user_data['current_state'] = ANIME_SELECT
        return await show_video_selection(update, context, matches)

async def show_video_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, matches):
    """显示影视选择列表"""
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
    
    message += "\n请输入序号选择影视：\n\n💡 发送 /retry 重新执行当前步骤"
    
    await update.message.reply_text(message)
    return ANIME_SELECT

async def handle_video_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理影视选择"""
    try:
        selection = int(update.message.text.strip())
        matches = context.user_data.get('anime_matches', [])
        
        if 1 <= selection <= len(matches):
            selected_anime = matches[selection - 1]
            context.user_data['selected_anime'] = selected_anime
            context.user_data['current_state'] = SOURCE_SELECT
            return await show_video_sources(update, context, selected_anime)
        else:
            await update.message.reply_text(
                f"❌ 请输入有效的序号 (1-{len(matches)})\n\n"
                "💡 发送 /retry 重新执行当前步骤"
            )
            return ANIME_SELECT
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入数字序号\n\n"
            "💡 发送 /retry 重新执行当前步骤"
        )
        return ANIME_SELECT

async def show_video_sources(update: Update, context: ContextTypes.DEFAULT_TYPE, anime):
    """显示影视源列表"""
    anime_id = anime.get('animeId')
    
    try:
        # 调用API获取源列表
        response = call_danmaku_api('GET', f'/library/anime/{anime_id}/sources')
        
        if not response or not response.get('success'):
            await update.message.reply_text(
                "❌ 获取影视源失败，请稍后重试\n\n"
                "💡 发送 /retry 重新执行当前步骤"
            )
            return SOURCE_SELECT
        
        sources = response.get('data', [])
        
        if not sources:
            await update.message.reply_text(
                "❌ 该影视暂无可用源\n\n"
                "💡 发送 /retry 重新执行当前步骤"
            )
            return SOURCE_SELECT
        
        # 保存源列表到上下文
        context.user_data['anime_sources'] = sources
        
        if len(sources) == 1:
            # 只有一个源，直接选择
            source = sources[0]
            context.user_data['selected_source'] = source
            context.user_data['current_state'] = EPISODE_INPUT
            return await request_episode_input(update, context, anime, source)
        else:
            # 多个源，让用户选择
            return await show_source_selection(update, context, anime, sources)
            
    except Exception as e:
        logger.error(f"获取影视源异常: {e}")
        await update.message.reply_text(
            "❌ 获取影视源时发生错误，请稍后重试\n\n"
            "💡 发送 /retry 重新执行当前步骤"
        )
        return SOURCE_SELECT

async def show_source_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, anime, sources):
    """显示源选择列表"""
    title = anime.get('title', '未知影视')
    message = f"📺 {title}\n\n找到 {len(sources)} 个可用源：\n\n"
    
    for i, source in enumerate(sources, 1):
        source_name = source.get('providerName', f'源{i}')
        episode_count = source.get('episodeCount', 0)
        
        info = f"{source_name}"
        if episode_count:
            info += f" [{episode_count}集]"
        
        message += f"{i}. {info}\n"
    
    message += "\n请输入序号选择源：\n\n💡 发送 /retry 重新执行当前步骤"
    
    await update.message.reply_text(message)
    return SOURCE_SELECT

async def handle_source_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理源选择"""
    try:
        selection = int(update.message.text.strip())
        sources = context.user_data.get('anime_sources', [])
        
        if 1 <= selection <= len(sources):
            selected_source = sources[selection - 1]
            context.user_data['selected_source'] = selected_source
            context.user_data['current_state'] = EPISODE_INPUT
            
            anime = context.user_data.get('selected_anime')
            return await request_episode_input(update, context, anime, selected_source)
        else:
            await update.message.reply_text(
                f"❌ 请输入有效的序号 (1-{len(sources)})\n\n"
                "💡 发送 /retry 重新执行当前步骤"
            )
            return SOURCE_SELECT
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入数字序号\n\n"
            "💡 发送 /retry 重新执行当前步骤"
        )
        return SOURCE_SELECT

async def request_episode_input(update: Update, context: ContextTypes.DEFAULT_TYPE, anime, source):
    """请求用户输入集数"""
    anime_title = anime.get('title', '未知影视')
    source_name = source.get('providerName', '未知源')
    episode_count = source.get('episodeCount', 0)
    
    message = f"📺 {anime_title}\n🎬 源: {source_name}\n\n"
    
    if episode_count > 0:
        message += f"该源共有 {episode_count} 集\n\n"
    
    message += "请输入要导入的集数：\n\n💡 发送 /retry 重新执行当前步骤"
    
    await update.message.reply_text(message)
    return EPISODE_INPUT

async def handle_episode_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理集数输入并执行导入"""
    try:
        episode_index = int(update.message.text.strip())
        
        if episode_index < 1:
            await update.message.reply_text(
                "❌ 集数必须大于0\n\n"
                "💡 发送 /retry 重新执行当前步骤"
            )
            return EPISODE_INPUT
        
        # 获取所有必要的参数
        url = context.user_data.get('import_url')
        source = context.user_data.get('selected_source')
        anime = context.user_data.get('selected_anime')
        
        source_id = source.get('sourceId') or source.get('id')
        
        if not all([url, source_id]):
            await update.message.reply_text(
                "❌ 缺少必要参数，请重新开始导入流程\n\n"
                "💡 发送 /retry 重新执行当前步骤"
            )
            return EPISODE_INPUT
        
        # 准备API请求参数
        import_data = {
            'sourceId': source_id,
            'episode_index': episode_index,
            'url': url
        }
        
        # 添加页面标题参数（如果成功解析到标题的话）
        page_title = context.user_data.get('page_title', '').strip()
        if page_title:  # 只有当标题不为空时才添加到API参数中
            import_data['title'] = page_title
        
        # 显示导入信息
        anime_title = anime.get('title', '未知影视')
        source_name = source.get('providerName', '未知源')
        
        await update.message.reply_text(
            f"🚀 开始导入...\n\n"
            f"📺 影视: {anime_title}\n"
            f"🎬 源: {source_name}\n"
            f"📊 集数: 第{episode_index}集\n"
            f"🔗 URL: {url}"
        )
        
        # 调用导入API
        try:
            response = call_danmaku_api('POST', '/import/url', None, import_data)
            
            if response and response.get('success'):
                await update.message.reply_text(
                    "✅ URL导入成功！\n\n"
                    "导入任务已提交，请稍后查看处理结果。"
                )
            else:
                error_msg = response.get('message', '未知错误') if response else '请求失败'
                await update.message.reply_text(
                    f"❌ 导入失败: {error_msg}\n\n"
                    "💡 发送 /retry 重新执行当前步骤"
                )
                return EPISODE_INPUT
        except Exception as e:
            logger.error(f"调用导入API异常: {e}")
            await update.message.reply_text(
                "❌ 导入时发生错误，请稍后重试\n\n"
                "💡 发送 /retry 重新执行当前步骤"
            )
            return EPISODE_INPUT
        
        # 清理用户数据
        context.user_data.clear()
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入有效的数字\n\n"
            "💡 发送 /retry 重新执行当前步骤"
        )
        return EPISODE_INPUT

async def cancel_import_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消URL导入"""
    context.user_data.clear()
    await update.message.reply_text("❌ URL导入已取消")
    return ConversationHandler.END

async def restart_import_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """在对话中重新开始URL导入流程"""
    # 重新开始URL导入流程
    return await import_url_start(update, context)

# 导出处理器创建函数
def create_import_url_handler():
    """创建URL导入对话处理器"""
    return ConversationHandler(
        entry_points=[
            CommandHandler('url', import_url_start),
        ],
        states={
            URL_INPUT: [
                CommandHandler('retry', retry_current_step),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url_input),
            ],
            KEYWORD_INPUT: [
                CommandHandler('retry', retry_current_step),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyword_input),
            ],
            ANIME_SELECT: [
                CommandHandler('retry', retry_current_step),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_selection),
            ],
            SOURCE_SELECT: [
                CommandHandler('retry', retry_current_step),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_source_selection),
            ],
            EPISODE_INPUT: [
                CommandHandler('retry', retry_current_step),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_episode_input),
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel_import_url),
            CommandHandler('search', cancel_import_url),
            CommandHandler('auto', cancel_import_url),
            CommandHandler('start', cancel_import_url),
            CommandHandler('help', cancel_import_url),
            CommandHandler('url', restart_import_url),
        ],
        name='import_url_conversation',
        persistent=False,
    )