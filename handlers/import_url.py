import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from utils.api import call_danmaku_api
from utils.permission import check_admin_permission
from utils.title_extractor import extract_show_title_from_h1
from utils.rate_limit import should_block_by_rate_limit
from utils.conversation_states import URL_INPUT, KEYWORD_INPUT, ANIME_SELECT, SOURCE_SELECT, EPISODE_INPUT
from utils.handlers_utils import wrap_conversation_entry_point
from utils.handlers_fallbacks import get_global_fallbacks

logger = logging.getLogger(__name__)

async def check_url_accessibility(url: str) -> tuple[bool, str, dict]:
    """检查URL是否可访问并解析详细信息
    
    Returns:
        tuple[bool, str, dict]: (是否可访问, 错误信息或状态描述, 页面详细信息)
    """
    # 更完整的请求头，特别针对bilibili等网站
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0'
    }
    
    try:
        # 对于某些网站（如bilibili），直接使用GET请求而不是HEAD
        # 因为HEAD请求可能被拒绝或返回不准确的状态码
        response = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
        
        if response.status_code == 200:
            page_info = {'page_title': '', 'episode_title': '', 'original_title': ''}
            try:
                # 正确处理字符编码
                response.encoding = response.apparent_encoding or 'utf-8'
                page_info = extract_detailed_info_from_html(response.text)
            except Exception:
                # 信息解析失败，但不影响URL可访问性判断
                pass
            return True, "URL可访问", page_info
        else:
            return False, f"HTTP {response.status_code}: {response.reason}", {'page_title': '', 'episode_title': '', 'original_title': ''}
            
    except requests.exceptions.Timeout:
        return False, "请求超时，URL可能无法访问", {'page_title': '', 'episode_title': '', 'original_title': ''}
    except requests.exceptions.ConnectionError:
        return False, "连接失败，URL无法访问", {'page_title': '', 'episode_title': '', 'original_title': ''}
    except requests.exceptions.InvalidURL:
        return False, "无效的URL格式", {'page_title': '', 'episode_title': '', 'original_title': ''}
    except requests.exceptions.TooManyRedirects:
        return False, "重定向次数过多", {'page_title': '', 'episode_title': '', 'original_title': ''}
    except Exception as e:
        return False, f"检查失败: {str(e)[:50]}", {'page_title': '', 'episode_title': '', 'original_title': ''}

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


def extract_detailed_info_from_html(html_content: str) -> dict:
    """从HTML内容中提取详细信息，包括标题和可能的集标题
    
    Args:
        html_content: HTML页面内容
        
    Returns:
        dict: 包含页面标题和集标题的字典
    """
    result = {
        'page_title': '',
        'episode_title': '',
        'original_title': '',
        'show_title': ''
    }
    
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 获取原始title标签内容
        title_tag = soup.find('title')
        if title_tag and title_tag.string:
            original_title = title_tag.string.strip()
            result['original_title'] = original_title
            
            # 提取集标题信息
            episode_title = extract_episode_title(original_title)
            result['episode_title'] = episode_title
            
            # 清理后的页面标题
            cleaned_title = clean_page_title(original_title)
            result['page_title'] = cleaned_title
        
        # 使用h1标签提取节目标题
        show_title = extract_show_title_from_h1(soup)
        result['show_title'] = show_title
    
    except Exception as e:
        logger.debug(f"提取页面信息时出错: {e}")
    
    return result

def extract_episode_title(title: str) -> str:
    """从页面标题中提取集标题信息
    
    Args:
        title: 原始页面标题
        
    Returns:
        str: 集标题，如果无法提取则返回空字符串
    """
    import re
    
    # 常见的集数标题模式
    episode_patterns = [
        # 匹配 "第X集" 或 "第X话" 后面的内容
        r'第\d+[集话]\s*[：:：]?\s*([^\|\-_]+)',
        # 匹配 "EP.X" 或 "Episode X" 后面的内容
        r'(?:EP\.?|Episode)\s*\d+\s*[：:：]?\s*([^\|\-_]+)',
        # 匹配数字后面跟着标题的模式
        r'\d+\s*[：:：]\s*([^\|\-_]+)',
        # 匹配括号中的集标题
        r'\(([^\)]+)\)',
        # 匹配引号中的集标题
        r'[""\']([^""\'])+[""\']'
    ]
    
    for pattern in episode_patterns:
        match = re.search(pattern, title)
        if match:
            episode_title = match.group(1).strip()
            # 过滤掉一些无意义的内容
            if len(episode_title) > 2 and not any(keyword in episode_title for keyword in 
                ['在线观看', '高清', '免费', '视频', '网站', 'bilibili', '腾讯', '爱奇艺', '优酷']):
                return episode_title
    
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



# 库缓存机制已移除，改为直接调用/library/search接口

def search_video_by_keyword(keyword, media_type=None):
    """通过调用/library/search接口搜索影视资源
    
    Args:
        keyword: 搜索关键词
        media_type: 媒体类型过滤 ('movie' 或 'tv_series')，None表示不过滤
    
    Returns:
        list: 匹配的影视列表
    """
    from utils.api import call_danmaku_api
    
    try:
        # 调用新的搜索接口
        api_result = call_danmaku_api(
            method="GET",
            endpoint="/library/search",
            params={"keyword": keyword}
        )
        
        if not api_result.get("success"):
            logger.error(f"搜索接口调用失败: {api_result.get('error', '未知错误')}")
            return []
        
        search_results = api_result.get("data", [])
        
        # 如果指定了媒体类型，进行类型过滤
        if media_type:
            filtered_results = []
            for anime in search_results:
                anime_type = anime.get('type', '').lower()
                if media_type == 'movie':
                    # 电影类型匹配
                    if anime_type in ['movie', '电影']:
                        filtered_results.append(anime)
                elif media_type == 'tv_series':
                    # 电视剧类型匹配（排除电影类型）
                    if anime_type not in ['movie', '电影']:
                        filtered_results.append(anime)
            return filtered_results
        
        return search_results
        
    except Exception as e:
        logger.error(f"搜索视频时发生异常: {e}")
        return []

def is_movie_source(anime):
    """检测影视是否为电影类型
    
    Args:
        anime: 影视信息字典
        
    Returns:
        bool: 如果是电影类型返回True，否则返回False
    """
    # 直接检查type字段
    anime_type = anime.get('type', '').lower()
    if anime_type == 'movie':
        return True
    
    # 兼容中文类型
    if anime_type == '电影':
        return True
    
    return False

async def auto_import_movie(update: Update, context: ContextTypes.DEFAULT_TYPE, anime, source):
    """自动导入电影（使用第1集）
    
    Args:
        update: Telegram更新对象
        context: 上下文对象
        anime: 选中的影视信息
        source: 选中的源信息
        
    Returns:
        int: ConversationHandler.END
    """
    # 获取所有必要的参数
    url = context.user_data.get('import_url')
    source_id = source.get('sourceId') or source.get('id')
    
    if not all([url, source_id]):
        await update.message.reply_text(
            "❌ 缺少必要参数，请重新开始导入流程"
        )
        return EPISODE_INPUT
    
    # 准备API请求参数（电影默认使用第1集）
    episode_index = 1
    import_data = {
        'sourceId': source_id,
        'episode_index': episode_index,
        'url': url
    }
    
    # 添加页面信息参数
    page_info = context.user_data.get('page_info', {})
    page_title = page_info.get('page_title', '').strip()
    episode_title = page_info.get('episode_title', '').strip()
    show_title = page_info.get('show_title', '').strip()
    
    if page_title:
        import_data['title'] = page_title
    
    if episode_title:  # 添加集标题参数
        import_data['episode_title'] = episode_title
    
    # 添加节目名称参数（优先使用从h1提取的节目标题）
    anime_name = show_title or anime.get('title', '')
    if anime_name:
        import_data['anime_name'] = anime_name
    
    # 显示导入信息
    anime_title = anime.get('title', '未知影视')
    source_name = source.get('providerName', '未知源')
    
    await update.message.reply_text(
        f"🎬 检测到电影类型，自动使用第1集进行导入\n\n"
        f"📺 影视: {anime_title}\n"
        f"🎬 源: {source_name}\n"
        f"📊 集数: 第{episode_index}集（电影）\n"
        f"🔗 URL: {url}"
    )
    
    # 调用导入API
    try:
        response = call_danmaku_api('POST', '/import/url', None, import_data)
        
        if response and response.get('success'):
            await update.message.reply_text(
                "✅ 电影导入成功！\n\n"
                "导入任务已提交，请稍后查看处理结果。"
            )
        else:
            error_msg = response.get('message', '未知错误') if response else '请求失败'
            await update.message.reply_text(
                f"❌ 导入失败: {error_msg}"
            )
            return EPISODE_INPUT
    except Exception as e:
        logger.error(f"调用导入API异常: {e}")
        await update.message.reply_text(
            "❌ 导入时发生错误，请稍后重试"
        )
        return EPISODE_INPUT
    
    # 清理用户数据
    context.user_data.clear()
    return ConversationHandler.END

@check_admin_permission
async def url_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始URL导入流程"""
    # 检查流控状态
    should_block, seconds_until_reset = should_block_by_rate_limit()
    if should_block:
        seconds_text = str(seconds_until_reset) if seconds_until_reset is not None else "稍后"
        await update.message.reply_text(f"🚫 无法URL导入，当前系统处于流控状态，请{seconds_text}秒后再试")
        return ConversationHandler.END
        
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
            # 检查URL可访问性并解析页面信息
            await update.message.reply_text("🔍 正在检查URL可访问性并解析页面信息...")
            
            is_accessible, status_msg, page_info = await check_url_accessibility(url)
            
            if is_accessible:
                # URL可访问，继续流程
                context.user_data['import_url'] = url
                context.user_data['page_info'] = page_info
                
                # 优先使用h1节目标题，如果没有则使用页面标题进行匹配
                show_title = page_info.get('show_title', '').strip()
                page_title = page_info.get('page_title', '').strip()
                
                # 选择最佳匹配关键词：优先使用h1节目标题
                match_keyword = show_title if show_title else page_title
                
                if match_keyword:
                    title_type = "节目标题" if show_title else "页面标题"
                    await update.message.reply_text(f"✅ URL验证成功: {url}\n📄 {title_type}: {match_keyword}\n\n🔍 正在尝试自动匹配影视库...")
                    
                    # 使用节目标题或页面标题搜索匹配的影视
                    matches = search_video_by_keyword(match_keyword)
                    
                    if matches:
                        if len(matches) == 1:
                            # 只有一个匹配结果，直接进入源选择
                            video = matches[0]
                            context.user_data['selected_anime'] = video
                            context.user_data['current_state'] = SOURCE_SELECT
                            await update.message.reply_text(f"🎯 自动匹配成功: {video.get('title', '未知标题')}")
                            return await show_video_sources(update, context, video)
                        else:
                            # 多个匹配结果，让用户选择
                            context.user_data['anime_matches'] = matches
                            context.user_data['current_state'] = ANIME_SELECT
                            await update.message.reply_text(f"🎯 找到 {len(matches)} 个可能的匹配结果")
                            return await show_video_selection(update, context, matches)
                    else:
                        # 自动匹配失败，进入手动输入流程
                        await update.message.reply_text(f"⚠️ 未能自动匹配到影视，请手动输入关键词搜索：")
                else:
                    # 没有页面标题，直接进入手动输入流程
                    await update.message.reply_text(f"✅ URL验证成功: {url}\n\n请输入关键词来搜索影视库：")
                
                context.user_data['current_state'] = KEYWORD_INPUT
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
        "• 可以直接使用：/url https://example.com/video"
    )
    return URL_INPUT

async def handle_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理URL输入"""
    url = update.message.text.strip()
    
    # 简单的URL验证
    if not (url.startswith('http://') or url.startswith('https://')):
        await update.message.reply_text(
            "❌ 请输入有效的URL（以http://或https://开头）"
        )
        return URL_INPUT
    
    # 检查URL可访问性并解析页面信息
    await update.message.reply_text("🔍 正在检查URL可访问性并解析页面信息...")
    
    is_accessible, status_msg, page_info = await check_url_accessibility(url)
    
    if is_accessible:
        # URL可访问，继续流程
        context.user_data['import_url'] = url
        context.user_data['page_info'] = page_info
        
        # 优先使用h1节目标题，如果没有则使用页面标题进行匹配
        show_title = page_info.get('show_title', '').strip()
        page_title = page_info.get('page_title', '').strip()
        
        # 选择最佳匹配关键词：优先使用h1节目标题
        match_keyword = show_title if show_title else page_title
        
        if match_keyword:
            title_type = "节目标题" if show_title else "页面标题"
            await update.message.reply_text(f"✅ URL验证成功: {url}\n📄 {title_type}: {match_keyword}\n\n🔍 正在尝试自动匹配影视库...")
            
            # 获取库数据
            # 使用节目标题或页面标题搜索匹配的影视
            matches = search_video_by_keyword(match_keyword)
            
            if matches:
                if len(matches) == 1:
                    # 只有一个匹配结果，直接进入源选择
                    video = matches[0]
                    context.user_data['selected_anime'] = video
                    context.user_data['current_state'] = SOURCE_SELECT
                    await update.message.reply_text(f"🎯 自动匹配成功: {video.get('title', '未知标题')}")
                    return await show_video_sources(update, context, video)
                else:
                    # 多个匹配结果，让用户选择
                    context.user_data['anime_matches'] = matches
                    context.user_data['current_state'] = ANIME_SELECT
                    await update.message.reply_text(f"🎯 找到 {len(matches)} 个可能的匹配结果")
                    return await show_video_selection(update, context, matches)
            else:
                # 自动匹配失败，进入手动输入流程
                await update.message.reply_text(f"⚠️ 未能自动匹配到影视，请手动输入关键词搜索：")
        else:
            # 没有页面标题，直接进入手动输入流程
            await update.message.reply_text(f"✅ URL验证成功: {url}\n\n请输入关键词来搜索影视库：")
        
        context.user_data['current_state'] = KEYWORD_INPUT
        return KEYWORD_INPUT
    else:
        # URL不可访问
        await update.message.reply_text(
            f"❌ URL无法访问: {url}\n\n"
            f"错误信息: {status_msg}\n\n"
            "请检查URL是否正确或稍后重试："
        )
        return URL_INPUT

async def handle_keyword_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理关键词输入并搜索影视"""
    keyword = update.message.text.strip()
    
    if not keyword:
        await update.message.reply_text(
            "❌ 请输入有效的关键词"
        )
        return KEYWORD_INPUT
    
    # 搜索匹配的影视
    matches = search_video_by_keyword(keyword)
    
    if not matches:
        await update.message.reply_text(
            f"❌ 未找到包含关键词 '{keyword}' 的影视\n\n"
            "请重新输入关键词："
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
    
    message += "\n请输入序号选择影视："
    
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
                f"❌ 请输入有效的序号 (1-{len(matches)})"
            )
            return ANIME_SELECT
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入数字序号"
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
                "❌ 获取影视源失败，请稍后重试"
            )
            return SOURCE_SELECT
        
        sources = response.get('data', [])
        
        if not sources:
            await update.message.reply_text(
                "❌ 该影视暂无可用源"
            )
            return SOURCE_SELECT
        
        # 保存源列表到上下文
        context.user_data['anime_sources'] = sources
        
        if len(sources) == 1:
            # 只有一个源，直接选择
            source = sources[0]
            context.user_data['selected_source'] = source
            
            # 检测是否为电影类型
            if is_movie_source(anime):
                # 电影类型，直接使用第1集进行导入
                context.user_data['current_state'] = EPISODE_INPUT
                return await auto_import_movie(update, context, anime, source)
            else:
                # 非电影类型，正常进入集数输入
                context.user_data['current_state'] = EPISODE_INPUT
                return await request_episode_input(update, context, anime, source)
        else:
            # 多个源，让用户选择
            return await show_source_selection(update, context, anime, sources)
            
    except Exception as e:
        logger.error(f"获取影视源异常: {e}")
        await update.message.reply_text(
            "❌ 获取影视源时发生错误，请稍后重试"
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
    
    message += "\n请输入序号选择源："
    
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
            
            # 检测是否为电影类型
            if is_movie_source(anime):
                # 电影类型，直接使用第1集进行导入
                return await auto_import_movie(update, context, anime, selected_source)
            else:
                # 非电影类型，正常进入集数输入
                return await request_episode_input(update, context, anime, selected_source)
        else:
            await update.message.reply_text(
                f"❌ 请输入有效的序号 (1-{len(sources)})"
            )
            return SOURCE_SELECT
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入数字序号"
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
    
    message += "请输入要导入的集数："
    
    await update.message.reply_text(message)
    return EPISODE_INPUT

async def handle_episode_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理集数输入并执行导入"""
    try:
        episode_index = int(update.message.text.strip())
        
        if episode_index < 1:
            await update.message.reply_text(
                "❌ 集数必须大于0"
            )
            return EPISODE_INPUT
        
        # 获取所有必要的参数
        url = context.user_data.get('import_url')
        source = context.user_data.get('selected_source')
        anime = context.user_data.get('selected_anime')
        
        source_id = source.get('sourceId') or source.get('id')
        
        if not all([url, source_id]):
            await update.message.reply_text(
                "❌ 缺少必要参数，请重新开始导入流程"
            )
            return EPISODE_INPUT
        
        # 准备API请求参数
        import_data = {
            'sourceId': source_id,
            'episode_index': episode_index,
            'url': url
        }
        
        # 添加页面信息参数
        page_info = context.user_data.get('page_info', {})
        page_title = page_info.get('page_title', '').strip()
        episode_title = page_info.get('episode_title', '').strip()
        show_title = page_info.get('show_title', '').strip()
        
        if page_title:  # 只有当标题不为空时才添加到API参数中
            import_data['title'] = page_title
        
        if episode_title:  # 添加集标题参数
            import_data['episode_title'] = episode_title
        
        # 添加节目名称参数（优先使用从h1提取的节目标题）
        anime_name = show_title or anime.get('title', '')
        if anime_name:
            import_data['anime_name'] = anime_name
        
        # 显示导入信息
        anime_title = anime.get('title', '未知影视')
        source_name = source.get('providerName', '未知源')
        
        await update.message.reply_text(
            f"🚀 导入信息:\n\n"
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
                    f"❌ 导入失败: {error_msg}"
                )
                return EPISODE_INPUT
        except Exception as e:
            logger.error(f"调用导入API异常: {e}")
            await update.message.reply_text(
                "❌ 导入时发生错误，请稍后重试"
            )
            return EPISODE_INPUT
        
        # 清理用户数据
        context.user_data.clear()
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text(
            "❌ 请输入有效的数字"
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
    return await url_command(update, context)

# 导出处理器创建函数
def create_import_url_handler():
    """创建URL导入对话处理器"""
    return ConversationHandler(
        entry_points=[
            CommandHandler('url', wrap_conversation_entry_point(url_command)),
        ],
        states={
            URL_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url_input),
            ],
            KEYWORD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyword_input),
            ],
            ANIME_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_selection),
            ],
            SOURCE_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_source_selection),
            ],
            EPISODE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_episode_input),
            ],
        },
        fallbacks=get_global_fallbacks(),
        name='import_url_conversation',
        per_chat=True,
        per_user=True,
        allow_reentry=True,
        persistent=False,
    )