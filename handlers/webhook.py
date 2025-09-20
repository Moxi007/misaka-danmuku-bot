import logging
import json
import os
import asyncio
import re
import uuid
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot
from config import ConfigManager
from handlers.import_url import search_video_by_keyword
from utils.tmdb_api import get_tmdb_media_details, search_tv_series_by_name_year, validate_tv_series_match
from utils.api import call_danmaku_api
from utils.security import mask_sensitive_data
from utils.emby_name_converter import convert_emby_series_name

logger = logging.getLogger(__name__)

class WebhookTask:
    """Webhook任务数据结构"""
    def __init__(self, webhook_id: str, operation_type: str, media_info: Dict[str, Any], 
                 message_id: int, chat_id: str):
        self.webhook_id = webhook_id
        self.operation_type = operation_type  # 'import' or 'refresh'
        self.media_info = media_info
        self.message_id = message_id  # Telegram消息ID
        self.chat_id = chat_id  # Telegram聊天ID
        self.task_ids: List[str] = []  # API返回的taskId列表
        self.task_statuses: Dict[str, str] = {}  # {taskId: task_status}
        self.created_at = datetime.now()
        self.completed = False

class WebhookHandler:
    """Webhook处理器，用于处理来自Emby等媒体服务器的通知"""
    
    def __init__(self, bot: Optional[Bot] = None):
        self.config = ConfigManager()
        self.bot = bot
        # 从环境变量读取时区配置，默认为Asia/Shanghai
        self.timezone = ZoneInfo(os.getenv('TZ', 'Asia/Shanghai'))
        self._tmdb_cache = {}  # TMDB搜索结果缓存
        self._play_event_cache = {}  # 播放事件缓存，避免重复处理
        
        # taskId轮询相关数据结构
        self._webhook_tasks = {}  # webhook任务记录: {webhook_id: WebhookTask}
        self._webhook_import_tasks = {}  # 入库任务记录: {import_task_id: webhook_id}
        self._polling_active = False  # 轮询状态标志
        self._polling_task = None  # 轮询任务引用
        
    def validate_api_key(self, provided_key: str) -> bool:
        """验证API密钥"""
        if not self.config.webhook.enabled:
            logger.warning("🔒 Webhook功能未启用，拒绝请求")
            return False
            
        if not provided_key:
            logger.warning("🔒 缺少API密钥")
            return False
            
        if provided_key != self.config.webhook.api_key:
            logger.warning(f"🔒 API密钥验证失败: {mask_sensitive_data(provided_key)}")
            return False
            
        return True
    
    async def handle_emby_webhook(self, data: Dict[str, Any], api_key: str) -> Dict[str, Any]:
        """处理Emby webhook通知
        
        Args:
            data: Emby发送的webhook数据
            api_key: 请求中的API密钥
            
        Returns:
            Dict[str, Any]: 响应数据
        """
        try:
            # 验证API密钥
            if not self.validate_api_key(api_key):
                return {
                    "success": False,
                    "error": "API密钥验证失败",
                    "code": 401
                }
            
            # 解析Emby通知数据
            event_type = data.get('Event', '')
            logger.info(f"📡 收到Emby通知，事件类型: {event_type}")
            
            # 记录完整的Emby消息体到日志（DEBUG级别）
            logger.debug(f"📋 完整Emby消息体:\n{json.dumps(data, indent=2, ensure_ascii=False)}")
            
            # 记录关键信息到INFO级别日志
            item_info = data.get('Item', {})
            session_info = data.get('Session', {})
            user_info = data.get('User', {})
            logger.info(f"📺 媒体信息: {item_info.get('Name', '未知')} (类型: {item_info.get('Type', '未知')})")
            logger.info(f"👤 用户信息: {user_info.get('Name', '未知')} | 设备: {session_info.get('DeviceName', '未知')} ({session_info.get('Client', '未知')})")
            logger.info(f"🔗 提供商ID: {item_info.get('ProviderIds', {})}")
            
            # 只处理播放开始事件
            if event_type != 'playback.start':
                logger.info(f"ℹ️ 忽略非播放开始事件: {event_type}")
                return {
                    "success": True,
                    "message": f"事件 {event_type} 已忽略",
                    "processed": False
                }
            
            # 提取媒体信息
            media_info = self._extract_media_info(data)
            if not media_info:
                logger.warning("⚠️ 无法提取媒体信息")
                return {
                    "success": False,
                    "error": "无法提取媒体信息",
                    "code": 400
                }
            
            # 记录播放事件
            tmdb_info = f" [TMDB: {media_info['tmdb_id']}]" if media_info.get('tmdb_id') else ""
            logger.info(
                f"🎬 Emby播放开始: {media_info['title']} "
                f"(用户: {media_info.get('user', '未知')}){tmdb_info}"
            )
            
            # 执行智能影视库管理流程
            await self._process_smart_library_management(media_info)
            
            # 如果配置了Telegram机器人，可以发送通知给管理员
            if self.bot and self.config.telegram.admin_user_ids:
                await self._send_play_notification(media_info)
            
            return {
                "success": True,
                "message": "播放开始事件已处理",
                "processed": True,
                "media_info": media_info
            }
            
        except Exception as e:
            logger.error(f"❌ 处理Emby webhook时发生错误: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"处理webhook时发生错误: {str(e)}",
                "code": 500
            }
    
    def _extract_media_info(self, data: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """从Emby webhook数据中提取媒体信息
        
        Args:
            data: Emby webhook数据
            
        Returns:
            Optional[Dict[str, str]]: 提取的媒体信息，如果提取失败则返回None
        """
        try:
            item = data.get('Item', {})
            session = data.get('Session', {})
            user = data.get('User', {})
            
            # 提取基本信息
            title = item.get('Name', '未知标题')
            media_type = item.get('Type', '未知类型')
            year = item.get('ProductionYear', '')
            
            # 对于剧集，提取季和集信息
            season_number = item.get('ParentIndexNumber')
            episode_number = item.get('IndexNumber')
            series_name = item.get('SeriesName')
            
            # 优化年份提取：优先使用PremiereDate
            if not year and 'PremiereDate' in item and item['PremiereDate']:
                try:
                    premiere_date = datetime.fromisoformat(item['PremiereDate'].replace('Z', '+00:00'))
                    year = premiere_date.year
                    logger.debug(f"📅 从PremiereDate提取年份: {year}")
                except Exception as e:
                    logger.debug(f"解析PremiereDate失败: {e}")
            
            # 优化剧集名称提取：从路径中补充信息
            if not series_name and 'Path' in data:
                path = data['Path']
                import os
                import re
                
                path_parts = [p for p in path.split('/') if p.strip()]
                if len(path_parts) >= 3:
                    # 通常剧集名在倒数第三个或第四个位置
                    for i in range(-4, -1):
                        if abs(i) <= len(path_parts):
                            potential_name = path_parts[i]
                            # 跳过明显的季度文件夹
                            if not re.match(r'^Season\s+\d+$', potential_name, re.IGNORECASE):
                                series_name = potential_name
                                logger.debug(f"📺 从路径提取剧集名: {series_name}")
                                break
                
                # 从文件名中提取季集信息（如果Item中没有）
                if (not season_number or not episode_number) and path:
                    filename = os.path.basename(path)
                    patterns = [
                        r'S(\d+)E(\d+)',  # S01E01
                        r'Season\s*(\d+).*Episode\s*(\d+)',  # Season 1 Episode 1
                        r'第(\d+)季.*第(\d+)集',  # 第1季第1集
                        r'(\d+)x(\d+)',  # 1x01
                    ]
                    
                    for pattern in patterns:
                        match = re.search(pattern, filename, re.IGNORECASE)
                        if match:
                            if not season_number:
                                season_number = int(match.group(1))
                                logger.debug(f"📊 从文件名提取季度: S{season_number}")
                            if not episode_number:
                                episode_number = int(match.group(2))
                                logger.debug(f"📊 从文件名提取集数: E{episode_number}")
                            break
                
                # 从路径中提取年份（如果Item中没有）
                if not year:
                    year_match = re.search(r'\b(19|20)\d{2}\b', path)
                    if year_match:
                        year = int(year_match.group())
                        logger.debug(f"📅 从路径提取年份: {year}")
            
            # 清理剧集名称
            if series_name:
                import re
                series_name = series_name.strip()
                # 移除常见的无用后缀
                series_name = re.sub(r'\s*\(\d{4}\)\s*$', '', series_name)  # 移除年份括号
                series_name = re.sub(r'\s*-\s*Season\s+\d+\s*$', '', series_name, flags=re.IGNORECASE)  # 移除季度后缀
            
            # 应用名称转换映射（如果是剧集且有必要信息）
            identify_matched = False  # 标记是否匹配了识别词
            if media_type == 'Episode' and series_name and season_number:
                try:
                    converted_result = convert_emby_series_name(series_name, season_number)
                    if converted_result:
                        logger.info(f"🔄 名称转换成功: '{series_name}' S{season_number:02d} -> '{converted_result['series_name']}' S{converted_result['season_number']:02d}")
                        series_name = converted_result['series_name']
                        season_number = converted_result['season_number']
                        identify_matched = True  # 标记匹配了识别词
                    else:
                        logger.debug(f"📝 未找到名称转换规则: '{series_name}' S{season_number:02d}")
                except Exception as e:
                    logger.warning(f"⚠️ 名称转换时发生错误: {e}，使用原始名称")
            
            # 提取Provider ID信息（Emby刮削后的元数据）
            provider_ids = item.get('ProviderIds', {})
            tmdb_id = provider_ids.get('Tmdb') or provider_ids.get('TheMovieDb')
            imdb_id = provider_ids.get('Imdb')
            tvdb_id = provider_ids.get('Tvdb') or provider_ids.get('TheTVDB')
            douban_id = provider_ids.get('Douban') or provider_ids.get('DoubanMovie')
            bangumi_id = provider_ids.get('Bangumi') or provider_ids.get('BGM')
            
            # 调试日志：显示提供商ID信息
            logger.debug(f"🔍 媒体提供商ID信息: {provider_ids}")
            logger.debug(f"🎯 提取的Provider ID: TMDB={tmdb_id}, IMDB={imdb_id}, TVDB={tvdb_id}, Douban={douban_id}, Bangumi={bangumi_id}")
            logger.debug(f"🎯 最终提取信息: 剧集='{series_name}', 季度={season_number}, 集数={episode_number}, 年份={year}, TMDB_ID={tmdb_id}")
            
            # 构建完整标题
            if media_type == 'Episode' and series_name:
                if season_number and episode_number:
                    full_title = f"{series_name} S{season_number:02d}E{episode_number:02d} - {title}"
                else:
                    full_title = f"{series_name} - {title}"
            else:
                full_title = f"{title} ({year})" if year else title
            
            return {
                "title": full_title,
                "original_title": title,
                "type": media_type,
                "year": str(year) if year else '',
                "series_name": series_name or '',
                "season": str(season_number) if season_number else '',
                "episode": str(episode_number) if episode_number else '',
                "tmdb_id": tmdb_id or '',
                "imdb_id": imdb_id or '',
                "tvdb_id": tvdb_id or '',
                "douban_id": douban_id or '',
                "bangumi_id": bangumi_id or '',
                "identify_matched": identify_matched,  # 添加识别词匹配标识
                "user": user.get('Name', '未知用户'),
                "client": session.get('Client', '未知客户端'),
                "device": session.get('DeviceName', '未知设备'),
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"❌ 提取媒体信息时发生错误: {e}")
            return None
    
    async def _send_play_notification(self, media_info: Dict[str, str]):
        """向管理员发送播放通知
        
        Args:
            media_info: 媒体信息
        """
        try:
            if not self.bot:
                return
                
            message = (
                f"🎬 **Emby播放通知**\n\n"
                f"📺 **媒体**: {media_info['title']}\n"
                f"👤 **用户**: {media_info['user']}\n"
                f"📱 **设备**: {media_info['device']} ({media_info['client']})\n"
                f"⏰ **时间**: {media_info['timestamp']}"
            )
            
            # 发送给所有管理员
            for admin_id in self.config.telegram.admin_user_ids:
                try:
                    await self.bot.send_message(
                        chat_id=admin_id,
                        text=message,
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logger.error(f"❌ 向管理员 {admin_id} 发送通知失败: {e}")
                    
        except Exception as e:
            logger.error(f"❌ 发送播放通知时发生错误: {e}")
    
    async def _process_smart_library_management(self, media_info: Dict[str, str]):
        """执行智能影视库管理流程
        
        Args:
            media_info: 媒体信息
        """
        try:
            # 检查是否为重复播放事件
            if self._is_duplicate_play_event(media_info, cooldown_hours=self.config.webhook.play_event_cooldown_hours):
                return  # 跳过重复处理
            
            # 记录播放事件
            self._record_play_event(media_info)
            
            media_type = media_info.get('type', '')
            title = media_info.get('title')
            
            # 获取优先级Provider信息
            provider_type, provider_id, search_type = self._get_priority_provider_info(media_info)
            
            # 详细检查缺失的信息
            missing_info = []
            if not provider_id:
                missing_info.append('Provider ID')
            if not title:
                missing_info.append('标题')
            
            # 对于电视剧，如果缺少Provider ID但有剧集名称，尝试通过名称搜索TMDB ID
            if not provider_id and media_type == 'Episode':
                series_name = media_info.get('series_name')
                year = media_info.get('year')
                if series_name:
                    logger.info(f"🔍 电视剧缺少Provider ID，尝试通过剧集名称搜索TMDB ID: {series_name} ({year})")
                    # 这里可以调用TMDB搜索API来获取TMDB ID
                    # 暂时先记录日志，后续可以扩展搜索功能
                    logger.debug(f"📺 剧集信息: 名称='{series_name}', 年份='{year}', 季数='{media_info.get('season')}', 集数='{media_info.get('episode')}'")
            
            # 如果仍然缺少关键信息，跳过智能管理
            if not provider_id and not title:
                logger.info(f"ℹ️ 媒体缺少必要信息（{', '.join(missing_info)}），跳过智能管理")
                logger.debug(f"🔍 媒体信息详情: Provider='{provider_type}:{provider_id}', 标题='{title}', 类型='{media_type}'")
                return
            elif not provider_id:
                logger.info(f"⚠️ 媒体缺少Provider ID但有标题信息，继续处理: {title}")
                logger.debug(f"🔍 媒体信息详情: Provider='{provider_type}:{provider_id}', 标题='{title}', 类型='{media_type}'")
            else:
                logger.info(f"✅ 使用优先级Provider: {provider_type.upper()} ID={provider_id}")
            
            # 更新media_info中的Provider信息
            media_info['selected_provider_type'] = provider_type
            media_info['selected_provider_id'] = provider_id
            media_info['selected_search_type'] = search_type
            
            # 根据媒体类型选择处理方式
            if media_type == 'Movie':
                await self._process_movie_management(media_info)
            elif media_type == 'Episode':
                await self._process_tv_management(media_info)
            else:
                logger.info(f"ℹ️ 不支持的媒体类型: {media_type}，跳过智能管理")
                
        except Exception as e:
            logger.error(f"❌ 智能影视库管理处理失败: {e}", exc_info=True)
    
    async def _process_movie_management(self, media_info: Dict[str, str]):
        """处理电影智能管理流程
        
        Args:
            media_info: 电影媒体信息
        """
        try:
            # 获取优先级 provider 信息
            provider_id = media_info.get('selected_provider_id')
            provider_type = media_info.get('selected_provider_type', 'tmdb')
            
            movie_title = media_info.get('original_title') or media_info.get('title')
            year = media_info.get('year', '')
            
            logger.info(f"🎬 开始电影智能管理: {movie_title} ({year}) ({provider_type.upper()}: {provider_id})")
            
            # 1. 检查库中的电影，使用电影名称进行匹配
            matches = search_video_by_keyword(movie_title, media_type='movie')
            
            # 电影严格匹配策略：优先完全匹配的标题
            exact_matches = [match for match in matches 
                           if match.get('title', '').lower() == movie_title.lower()]
            
            if not exact_matches:
                # 未找到精确匹配：检查是否为识别词匹配
                identify_matched = media_info.get('identify_matched', False)
                if identify_matched:
                    # 识别词匹配时直接使用关键词导入
                    logger.info(f"🎯 识别词匹配且库中无对应资源，直接使用关键词导入: {movie_title}")
                    await self._import_movie_by_provider(None, 'keyword', movie_title, identify_matched)
                elif provider_id:
                    # 非识别词匹配时使用优先级 provider ID 自动导入电影
                    logger.info(f"📥 未找到匹配的电影，开始自动导入: {movie_title} ({year}) 使用 {provider_type.upper()} ID")
                    await self._import_movie_by_provider(provider_id, provider_type, movie_title, identify_matched)
                else:
                    logger.warning(f"⚠️ 无法导入电影，缺少有效的 provider ID: {movie_title}")
            else:
                # 存在匹配项：使用refresh功能更新电影数据
                selected_match = exact_matches[0]
                logger.info(f"🔄 找到匹配的电影，开始刷新: {selected_match.get('title', movie_title)}")
                
                # 获取源列表进行刷新
                anime_id = selected_match.get('animeId')
                refresh_success = False
                if anime_id:
                    try:
                        sources_response = call_danmaku_api('GET', f'/library/anime/{anime_id}/sources')
                        if sources_response and sources_response.get('success'):
                            sources = sources_response.get('data', [])
                            if sources:
                                source_id = sources[0].get('sourceId')
                                if source_id:
                                    await self._refresh_movie(source_id, selected_match.get('title', movie_title))
                                    refresh_success = True
                                else:
                                    logger.error(f"❌ 无法获取源ID: {selected_match.get('title')}")
                            else:
                                logger.warning(f"⚠️ 未找到可用源: {selected_match.get('title')}")
                        else:
                            logger.error(f"❌ 获取源列表失败: {selected_match.get('title')}")
                    except Exception as e:
                        logger.warning(f"⚠️ 刷新电影时发生错误，可能资源已被删除: {e}")
                        logger.info(f"💡 由于资源库缓存，将继续执行TMDB智能识别")
                else:
                    logger.error(f"❌ 无法获取动漫ID: {selected_match.get('title')}")
                
                # 如果刷新失败，继续执行TMDB智能识别和导入
                if not refresh_success:
                    identify_matched = media_info.get('identify_matched', False)
                    await self._fallback_tmdb_search_and_import(movie_title, year, media_type='movie', 
                                                               provider_id=provider_id, provider_type=provider_type, identify_matched=identify_matched)
                    
        except Exception as e:
            logger.error(f"❌ 电影智能管理处理失败: {e}", exc_info=True)
    
    async def _process_tv_management(self, media_info: Dict[str, str]):
        """处理电视剧智能管理流程
        
        Args:
            media_info: 电视剧媒体信息
        """
        try:
            # 获取优先级 provider 信息
            provider_id = media_info.get('selected_provider_id')
            provider_type = media_info.get('selected_provider_type', 'tmdb')
            
            series_name = media_info.get('series_name') or media_info.get('title')
            season = media_info.get('season')
            episode = media_info.get('episode')
            year = media_info.get('year', '')
            
            if not series_name:
                logger.info("ℹ️ 电视剧缺少剧集名称，跳过智能管理")
                return
            
            # 确保season和episode是整数类型
            try:
                season = int(season) if season else 0
                episode = int(episode) if episode else 0
            except (ValueError, TypeError):
                logger.warning(f"⚠️ 无效的季集编号: season={season}, episode={episode}")
                season = 0
                episode = 0
            
            logger.info(f"🤖 开始电视剧智能管理: {series_name} {'S' + str(season).zfill(2) if season else ''}{('E' + str(episode).zfill(2)) if episode else ''} ({provider_type.upper() if provider_type else 'NONE'}: {provider_id})")
            
            # 使用剧名搜索电视剧类型的内容
            matches = search_video_by_keyword(series_name, 'tv_series')
            logger.info(f"📊 剧名搜索结果: {len(matches)} 个")
            
            # 计算匹配分数并筛选，重点关注season字段匹配
            season_matches = []
            for match in matches:
                match_title = match.get('title', '').lower()
                match_season = match.get('season', '')
                series_name_lower = series_name.lower()
                score = 0
                
                # 名称匹配评分
                if series_name_lower == match_title:
                    score += 100  # 完全匹配
                elif series_name_lower in match_title:
                    score += 70   # 包含匹配
                elif match_title in series_name_lower:
                    score += 50   # 被包含匹配
                    
                # 季度字段匹配评分（使用专门的season字段）
                if season and match_season:
                    try:
                        match_season_num = int(match_season)
                        if match_season_num == season:
                            score += 100  # 季度完全匹配
                            logger.debug(f"✅ 季度完全匹配: {match_title} S{season}")
                        elif abs(match_season_num - season) <= 1:
                            score += 50   # 季度相近匹配
                            logger.debug(f"⚠️ 季度相近匹配: {match_title} S{match_season_num} vs S{season}")
                    except (ValueError, TypeError):
                        # 如果season字段不是数字，尝试字符串匹配
                        if str(season) in str(match_season):
                            score += 80
                            logger.debug(f"📝 季度字符串匹配: {match_title} season={match_season}")
                elif not season and not match_season:
                    # 都没有季度信息，给予基础分数
                    score += 20
                        
                # 年份匹配评分
                if year:
                    match_year = match.get('year', '')
                    if match_year and str(year) == str(match_year):
                        score += 30
                    
                if score > 60:  # 只添加高匹配度的结果
                    season_matches.append({'match': match, 'score': score})
                    logger.debug(f"📊 匹配项: {match_title} (season={match_season}) 分数={score}")
                    
            # 按匹配分数排序
            season_matches.sort(key=lambda x: x['score'], reverse=True)
            season_matches = [item['match'] for item in season_matches]
            
            logger.info(f"📊 Library匹配结果: 找到 {len(season_matches)} 个匹配项（基于season字段匹配）")
            if season_matches:
                for i, match in enumerate(season_matches[:3]):  # 只显示前3个
                    logger.info(f"  {i+1}. {match.get('title')} (season={match.get('season')}, ID: {match.get('animeId')})")
                        
            # 检查是否有完全匹配的季度
            exact_season_match = False
            if season_matches and season:
                for match in season_matches:
                    match_season = match.get('season', '')
                    try:
                        if int(match_season) == season:
                            exact_season_match = True
                            break
                    except (ValueError, TypeError):
                        if str(season) in str(match_season):
                            exact_season_match = True
                            break
            
            # 如果没有找到季度匹配、没有完全匹配的季度或未匹配到具体集数，尝试通过TMDB API搜索
            # 但如果识别词匹配，则跳过TMDB搜索直接使用关键词导入
            identify_matched = media_info.get('identify_matched', False)
            should_search_tmdb = (
                not season_matches or 
                (season and not exact_season_match) or 
                not episode
            ) and not provider_id and not identify_matched
            
            # 如果识别词匹配但库中无对应资源，直接使用关键词导入
            if identify_matched and not season_matches:
                logger.info(f"🎯 识别词匹配且库中无对应资源，直接使用关键词导入: {series_name}")
                await self._import_episodes_by_provider(None, 'keyword', season, [episode, episode + 1] if episode else None, series_name, identify_matched)
                return True
            
            if should_search_tmdb:
                logger.info(f"🔍 触发TMDB搜索原因: 无匹配项={not season_matches}, 季度不匹配={season and not exact_season_match}, 无集数={not episode}")
                
                # 先检查缓存
                cached_result = self._get_cached_tmdb_result(series_name)
                tmdb_search_result = None
                
                if cached_result:
                    logger.info(f"💾 使用缓存的TMDB结果: {series_name}")
                    tmdb_search_result = cached_result
                else:
                    logger.info(f"🔍 开始TMDB搜索: {series_name} ({year if year else '年份未知'})")
                    tmdb_search_result = search_tv_series_by_name_year(series_name, year)
                    
                    if tmdb_search_result:
                        # 缓存搜索结果
                        self._cache_tmdb_result(series_name, tmdb_search_result)
                
                if tmdb_search_result:
                    # 增强的匹配验证
                    match_score = self._calculate_match_score(tmdb_search_result, series_name, year, season)
                    logger.info(f"📊 TMDB匹配评分: {tmdb_search_result.get('name')} ({tmdb_search_result.get('year', 'N/A')}) - {match_score}分")
                    
                    if match_score >= 70:  # 设置合理的匹配阈值
                        found_tmdb_id = tmdb_search_result.get('tmdb_id')
                        logger.info(f"✅ TMDB搜索匹配成功: {tmdb_search_result.get('name')} - 匹配分数: {match_score}")
                        logger.info(f"📥 开始自动导入: {series_name} S{season} (TMDB: {found_tmdb_id})")
                        await self._import_episodes_by_provider(found_tmdb_id, 'tmdb', season, [episode, episode + 1] if episode else None, series_name)
                        return True
                    else:
                        logger.info(f"❌ TMDB搜索结果匹配度不足: {tmdb_search_result.get('name')} - 匹配分数: {match_score}")
                else:
                    logger.info(f"❌ TMDB搜索未找到结果: {series_name}")
            
            # 如果通过季度匹配到多个结果，执行严格匹配策略
            final_matches = []
            if season_matches:
                # 严格匹配：完全匹配剧集名称
                for match in season_matches:
                    match_title = match.get('title', '').lower()
                    # 移除季度信息后进行比较
                    clean_match_title = match_title.replace(f'season {season}', '').replace(f's{season}', '')\
                                      .replace(f'第{season}季', '').replace(f'第{season}部', '').strip()
                    clean_series_name = series_name.lower().strip()
                    
                    if clean_match_title == clean_series_name:
                        final_matches.append(match)
                        break  # 找到完全匹配就停止
                
                # 如果没有完全匹配，使用第一个季度匹配结果
                if not final_matches:
                    final_matches = [season_matches[0]]
            else:
                # 如果没有季度匹配，尝试完全匹配
                for match in matches:
                    match_title = match.get('title', '').lower().strip()
                    if match_title == series_name.lower().strip():
                        final_matches.append(match)
                        break
            
            if not final_matches:
                # 未找到匹配项：检查是否有 provider ID 进行自动导入
                if provider_id:
                    logger.info(f"📥 未找到匹配项，开始自动导入: {series_name} S{season} ({provider_type.upper()}: {provider_id})")
                    identify_matched = media_info.get('identify_matched', False)
                    await self._import_episodes_by_provider(provider_id, provider_type, season, [episode, episode + 1] if episode else None, series_name, identify_matched)
                else:
                    # 尝试通过TMDB API搜索获取TMDB ID
                    logger.info(f"🔍 未找到匹配项且缺少 provider ID，尝试通过TMDB搜索: {series_name} ({year})")
                    tmdb_search_result = search_tv_series_by_name_year(series_name, year)
                    
                    if tmdb_search_result:
                        # 验证搜索结果是否匹配
                        if validate_tv_series_match(tmdb_search_result, series_name, year, season, episode):
                            found_tmdb_id = tmdb_search_result.get('tmdb_id')
                            logger.info(f"✅ TMDB搜索成功，找到匹配的剧集: {tmdb_search_result.get('name')} (ID: {found_tmdb_id})")
                            logger.info(f"📥 开始自动导入: {series_name} S{season} (TMDB: {found_tmdb_id})")
                            identify_matched = media_info.get('identify_matched', False)
                            await self._import_episodes_by_provider(found_tmdb_id, 'tmdb', season, [episode, episode + 1] if episode else None, series_name, identify_matched)
                        else:
                            logger.warning(f"⚠️ TMDB搜索结果验证失败: {series_name}")
                            logger.debug(f"💡 建议: 请检查剧集名称和年份是否正确，或在Emby中添加正确的TMDB刮削信息")
                    else:
                        logger.info(f"ℹ️ TMDB搜索未找到匹配结果: {series_name} ({year})")
                        logger.debug(f"💡 建议: 请检查剧集名称和年份是否正确，或在Emby中添加TMDB刮削信息")
            else:
                # 存在匹配项：使用refresh功能更新
                selected_match = final_matches[0]
                logger.info(f"🔄 找到匹配项，开始刷新: {selected_match.get('title', series_name)} S{season}")
                
                # 获取源列表进行刷新
                anime_id = selected_match.get('animeId')
                refresh_success = False
                if anime_id:
                    try:
                        sources_response = call_danmaku_api('GET', f'/library/anime/{anime_id}/sources')
                        if sources_response and sources_response.get('success'):
                            sources = sources_response.get('data', [])
                            if sources:
                                source_id = sources[0].get('sourceId')
                                if source_id:
                                    # 传递剧集名称和年份，用于TMDB搜索
                                    identify_matched = media_info.get('identify_matched', False)
                                    await self._refresh_episodes(source_id, [episode, episode + 1], provider_id, season, series_name, year, identify_matched)
                                    refresh_success = True
                                else:
                                    logger.error(f"❌ 无法获取源ID: {selected_match.get('title')}")
                            else:
                                logger.warning(f"⚠️ 未找到可用源: {selected_match.get('title')}")
                        else:
                            logger.error(f"❌ 获取源列表失败: {selected_match.get('title')}")
                    except Exception as e:
                        logger.warning(f"⚠️ 刷新剧集时发生错误，可能资源已被删除: {e}")
                        logger.info(f"💡 由于资源库缓存，将继续执行TMDB智能识别")
                else:
                    logger.error(f"❌ 无法获取资源ID: {selected_match.get('title')}")
                
                # 如果刷新失败，继续执行TMDB智能识别
                if not refresh_success:
                    identify_matched = media_info.get('identify_matched', False)
                    await self._fallback_tmdb_search_and_import(series_name, year, season, episode, 'tv',
                                                               provider_id=provider_id, provider_type=provider_type, identify_matched=identify_matched)
                    
        except Exception as e:
            logger.error(f"❌ 电视剧智能管理处理失败: {e}", exc_info=True)
    
    async def _fallback_tmdb_search_and_import(self, title: str, year: str = None, season: int = None, episode: int = None, 
                                             media_type: str = 'tv', provider_id: str = None, provider_type: str = None, identify_matched: bool = False):
        """TMDB辅助查询和导入的通用方法
        
        Args:
            title: 媒体标题
            year: 年份
            season: 季度（仅电视剧）
            episode: 集数（仅电视剧）
            media_type: 媒体类型 ('tv' 或 'movie')
            provider_id: 优先级provider ID
            provider_type: 优先级provider类型
        """
        try:
            # 如果识别词匹配，直接使用关键词导入，跳过TMDB搜索
            if identify_matched:
                logger.info(f"🎯 识别词匹配，直接使用关键词导入: {title}")
                if media_type == 'movie':
                    await self._import_movie_by_provider(None, 'keyword', title, identify_matched)
                    return
                elif media_type == 'tv':
                    await self._import_episodes_by_provider(None, 'keyword', season, [episode, episode + 1] if episode else None, title, identify_matched)
                    return
            
            # 优先使用provider信息进行导入
            if provider_id and provider_type:
                logger.info(f"📥 使用优先级provider进行导入: {title} ({provider_type.upper()}: {provider_id})")
                if media_type == 'movie':
                    await self._import_movie_by_provider(provider_id, provider_type, title, identify_matched)
                    return
                elif media_type == 'tv':
                    await self._import_episodes_by_provider(provider_id, provider_type, season, None, title, identify_matched)
                    return
            
            if media_type == 'movie':
                logger.info(f"🔍 刷新失败，开始TMDB智能识别: {title} ({year})")
                
                # 触发TMDB搜索逻辑
                cached_result = self._get_cached_tmdb_result(title)
                tmdb_search_result = None
                
                if cached_result:
                    logger.info(f"💾 使用缓存的TMDB结果: {title}")
                    tmdb_search_result = cached_result
                else:
                    logger.info(f"🔍 开始TMDB搜索: {title} ({year if year else '年份未知'})")
                    from utils.tmdb_api import search_movie_by_name_year
                    tmdb_search_result = search_movie_by_name_year(title, year)
                    
                    if tmdb_search_result:
                        # 缓存搜索结果
                        self._cache_tmdb_result(title, tmdb_search_result)
                
                if tmdb_search_result:
                    # 增强的匹配验证
                    match_score = self._calculate_movie_match_score(tmdb_search_result, title, year)
                    logger.info(f"📊 TMDB电影匹配评分: {tmdb_search_result.get('title')} ({tmdb_search_result.get('year', 'N/A')}) - {match_score}分")
                    
                    if match_score >= 70:  # 设置合理的匹配阈值
                        found_tmdb_id = tmdb_search_result.get('tmdb_id')
                        logger.info(f"✅ TMDB电影搜索匹配成功: {tmdb_search_result.get('title')} - 匹配分数: {match_score}")
                        
                        # 使用TMDB ID导入电影
                        await self._import_movie_by_tmdb_id(found_tmdb_id)
                        return
                    else:
                        logger.info(f"⚠️ TMDB电影匹配分数过低({match_score}分)，尝试fallback方案")
                
                # TMDB搜索失败或匹配分数过低
                logger.warning(f"⚠️ TMDB搜索失败，跳过导入: {title}")
                return
            
            if media_type == 'tv':
                logger.info(f"🔍 刷新失败，开始TMDB智能识别: {title} ({year})")
                
                # 触发TMDB搜索逻辑
                cached_result = self._get_cached_tmdb_result(title)
                tmdb_search_result = None
                
                if cached_result:
                    logger.info(f"💾 使用缓存的TMDB结果: {title}")
                    tmdb_search_result = cached_result
                else:
                    logger.info(f"🔍 开始TMDB搜索: {title} ({year if year else '年份未知'})")
                    tmdb_search_result = search_tv_series_by_name_year(title, year)
                    
                    if tmdb_search_result:
                        # 缓存搜索结果
                        self._cache_tmdb_result(title, tmdb_search_result)
                
                if tmdb_search_result:
                    # 增强的匹配验证
                    match_score = self._calculate_match_score(tmdb_search_result, title, year, season)
                    logger.info(f"📊 TMDB匹配评分: {tmdb_search_result.get('name')} ({tmdb_search_result.get('year', 'N/A')}) - {match_score}分")
                    
                    if match_score >= 70:  # 设置合理的匹配阈值
                        found_tmdb_id = tmdb_search_result.get('tmdb_id')
                        logger.info(f"✅ TMDB搜索匹配成功: {tmdb_search_result.get('name')} - 匹配分数: {match_score}")
                        logger.info(f"📥 开始自动导入: {title} S{season} (TMDB: {found_tmdb_id})")
                        await self._import_episodes_by_provider(found_tmdb_id, 'tmdb', season, [episode, episode + 1] if episode else None, title)
                    else:
                        logger.warning(f"⚠️ TMDB搜索结果验证失败: {title}")
                else:
                    logger.info(f"ℹ️ TMDB搜索未找到匹配结果: {title} ({year})")
                    
        except Exception as e:
            logger.error(f"❌ TMDB辅助查询处理失败: {e}", exc_info=True)
    
    def _calculate_movie_match_score(self, tmdb_result: dict, movie_title: str, year: str = None) -> int:
        """计算电影TMDB匹配评分
        
        Args:
            tmdb_result: TMDB搜索结果
            movie_title: 原始电影标题
            year: 年份（可选）
            
        Returns:
            匹配评分 (0-100)
        """
        if not tmdb_result:
            return 0
        
        score = 0
        tmdb_title = tmdb_result.get('title', '')
        tmdb_original_title = tmdb_result.get('original_title', '')
        tmdb_year = tmdb_result.get('year', '')
        
        # 标题匹配评分 (最高60分)
        movie_title_lower = movie_title.lower().strip()
        tmdb_title_lower = tmdb_title.lower().strip()
        tmdb_original_title_lower = tmdb_original_title.lower().strip()
        
        if movie_title_lower == tmdb_title_lower or movie_title_lower == tmdb_original_title_lower:
            score += 60  # 完全匹配
        elif movie_title_lower in tmdb_title_lower or tmdb_title_lower in movie_title_lower:
            score += 40  # 包含匹配
        elif movie_title_lower in tmdb_original_title_lower or tmdb_original_title_lower in movie_title_lower:
            score += 35  # 原标题包含匹配
        else:
            # 计算字符串相似度
            similarity = self._calculate_string_similarity(movie_title_lower, tmdb_title_lower)
            score += int(similarity * 30)  # 相似度匹配，最高30分
        
        # 年份匹配评分 (最高30分)
        if year and tmdb_year:
            try:
                year_diff = abs(int(year) - int(tmdb_year))
                if year_diff == 0:
                    score += 30  # 年份完全匹配
                elif year_diff == 1:
                    score += 20  # 年份相差1年
                elif year_diff <= 2:
                    score += 10  # 年份相差2年内
                # 年份相差超过2年不加分
            except (ValueError, TypeError):
                pass
        elif not year:
            score += 15  # 没有年份信息，给予中等分数
        
        # 受欢迎度加分 (最高10分)
        popularity = tmdb_result.get('popularity', 0)
        if popularity > 50:
            score += 10
        elif popularity > 20:
            score += 5
        elif popularity > 5:
            score += 2
        
        return min(score, 100)  # 确保不超过100分
    
    def _calculate_match_score(self, tmdb_result: dict, series_name: str, year: Optional[str], season: Optional[int]) -> int:
        """计算TMDB搜索结果的匹配分数
        
        Args:
            tmdb_result: TMDB搜索结果
            series_name: 剧集名称
            year: 年份
            season: 季度
            
        Returns:
            匹配分数 (0-200)
        """
        import time
        
        score = 0
        tmdb_name = tmdb_result.get('name', '').lower()
        tmdb_original_name = tmdb_result.get('original_name', '').lower()
        series_name_lower = series_name.lower()
        
        # 名称匹配评分 (最高100分)
        if series_name_lower == tmdb_name or series_name_lower == tmdb_original_name:
            score += 100  # 完全匹配
        elif series_name_lower in tmdb_name or series_name_lower in tmdb_original_name:
            score += 70   # 包含匹配
        elif tmdb_name in series_name_lower or tmdb_original_name in series_name_lower:
            score += 50   # 被包含匹配
        
        # 年份匹配评分 (最高30分)
        if year and tmdb_result.get('year'):
            tmdb_year = int(tmdb_result.get('year'))
            input_year = int(year)
            if tmdb_year == input_year:
                score += 30  # 年份完全匹配
            elif abs(tmdb_year - input_year) <= 1:
                score += 15  # 年份相差1年
        
        # 季度验证评分 (最高20分)
        if season and tmdb_result.get('number_of_seasons'):
            number_of_seasons = tmdb_result.get('number_of_seasons', 0)
            if number_of_seasons >= season:
                score += 20  # 季度数量合理
        
        return score
    
    def _cache_tmdb_result(self, series_name: str, tmdb_result: dict) -> None:
        """缓存TMDB搜索结果
        
        Args:
            series_name: 剧集名称
            tmdb_result: TMDB搜索结果
        """
        import time
        
        cache_key = series_name.lower().strip()
        self._tmdb_cache[cache_key] = {
            'result': tmdb_result,
            'timestamp': time.time()
        }
        logger.debug(f"💾 缓存TMDB搜索结果: {series_name} -> {tmdb_result.get('name')}")
    
    def _get_cached_tmdb_result(self, series_name: str) -> Optional[dict]:
        """获取缓存的TMDB搜索结果
        
        Args:
            series_name: 剧集名称
            
        Returns:
            缓存的TMDB结果，如果不存在或过期则返回None
        """
        import time
        
        cache_key = series_name.lower().strip()
        cached = self._tmdb_cache.get(cache_key)
        
        if cached:
            # 检查缓存是否过期 (24小时)
            if time.time() - cached['timestamp'] < 86400:
                logger.debug(f"💾 使用缓存的TMDB结果: {series_name}")
                return cached['result']
            else:
                # 清理过期缓存
                del self._tmdb_cache[cache_key]
                logger.debug(f"🗑️ 清理过期TMDB缓存: {series_name}")
        
        return None
    
    def _generate_media_key(self, media_info: Dict[str, str]) -> str:
        """生成媒体唯一标识符
        
        Args:
            media_info: 媒体信息
            
        Returns:
            str: 媒体唯一标识符
        """
        # 优先使用Provider ID作为唯一标识
        provider_ids = []
        for provider in ['tmdb_id', 'imdb_id', 'tvdb_id', 'douban_id', 'bangumi_id']:
            if media_info.get(provider):
                provider_ids.append(f"{provider}:{media_info[provider]}")
        
        if provider_ids:
            base_key = "|".join(provider_ids)
        else:
            # 如果没有Provider ID，使用标题和年份
            title = media_info.get('title', '').lower().strip()
            year = media_info.get('year', '')
            base_key = f"title:{title}|year:{year}"
        
        # 对于电视剧，添加季度和集数信息
        if media_info.get('type') == 'Episode':
            season = media_info.get('season', '')
            episode = media_info.get('episode', '')
            base_key += f"|season:{season}|episode:{episode}"
        
        return base_key
    
    def _is_duplicate_play_event(self, media_info: Dict[str, str], cooldown_hours: Optional[int] = None) -> bool:
        """检查是否为重复的播放事件
        
        Args:
            media_info: 媒体信息
            cooldown_hours: 冷却时间（小时），默认1小时
            
        Returns:
            bool: 如果是重复事件返回True
        """
        import time
        
        # 使用传入的冷却时间或配置文件中的默认值
        if cooldown_hours is None:
            cooldown_hours = self.config.webhook.play_event_cooldown_hours
        
        media_key = self._generate_media_key(media_info)
        current_time = time.time()
        cooldown_seconds = cooldown_hours * 3600
        
        # 检查缓存中是否存在该媒体的最近播放记录
        if media_key in self._play_event_cache:
            last_play_time = self._play_event_cache[media_key]
            if current_time - last_play_time < cooldown_seconds:
                logger.info(f"⏰ 检测到重复播放事件，跳过处理: {media_info.get('title')} (冷却中，剩余 {int((cooldown_seconds - (current_time - last_play_time)) / 60)} 分钟)")
                return True
        
        return False
    
    def _record_play_event(self, media_info: Dict[str, str]) -> None:
        """记录播放事件
        
        Args:
            media_info: 媒体信息
        """
        import time
        
        media_key = self._generate_media_key(media_info)
        current_time = time.time()
        
        # 记录播放时间
        self._play_event_cache[media_key] = current_time
        
        # 清理过期的缓存记录（超过24小时）
        expired_keys = []
        for key, timestamp in self._play_event_cache.items():
            if current_time - timestamp > 86400:  # 24小时
                expired_keys.append(key)
        
        for key in expired_keys:
            del self._play_event_cache[key]
        
        logger.debug(f"📝 记录播放事件: {media_info.get('title')} (缓存大小: {len(self._play_event_cache)})")
    
    async def _import_movie_by_tmdb_id(self, tmdb_id: str):
        """使用TMDB ID导入电影
        
        Args:
            tmdb_id: TMDB电影ID
        """
        try:
            logger.info(f"📥 开始导入电影 (TMDB: {tmdb_id})")
            
            # 调用导入API
            import_params = {
                "searchType": "tmdb",
                "searchTerm": tmdb_id,
                "originalKeyword": f"TMDB ID: {tmdb_id}"  # 添加原始关键词用于识别词匹配
            }
            
            response = call_danmaku_api('POST', '/import/auto', params=import_params)
            
            # 添加详细的API响应日志
            logger.info(f"🔍 电影导入 /import/auto API响应: {response}")
            
            if response and response.get('success'):
                # 从data字段中获取taskId
                data = response.get('data', {})
                task_id = data.get('taskId')
                logger.info(f"📊 响应data字段: {data}")
                logger.info(f"✅ 电影导入成功 (TMDB: {tmdb_id}), taskId: {task_id}")
                
                # 导入成功后刷新library缓存
                # 库缓存刷新已移除，改为直接调用/library/search接口
                logger.info("✅ 电影导入成功")
            else:
                error_msg = response.get('message', '未知错误') if response else '请求失败'
                logger.error(f"❌ 电影导入失败 (TMDB: {tmdb_id}): {error_msg}")
                
        except Exception as e:
            logger.error(f"❌ 导入电影时发生错误 (TMDB: {tmdb_id}): {e}", exc_info=True)
    
    async def _import_movie_by_provider(self, provider_id: str, provider_type: str = 'tmdb', movie_title: str = None, identify_matched: bool = False):
        """使用优先级 provider 导入单个电影
        
        Args:
            provider_id: Provider ID (tmdb_id, tvdb_id, imdb_id, douban_id, 或 bangumi_id)
            provider_type: Provider 类型 ('tmdb', 'tvdb', 'imdb', 'douban', 'bangumi')
            movie_title: 电影标题（可选，用于通知显示）
        """
        try:
            logger.info(f"📥 开始导入电影 ({provider_type.upper()}: {provider_id})")
            
            # 调用导入API
            if identify_matched and movie_title:
                # 识别词匹配时使用关键词模式
                import_params = {
                    "searchType": "keyword",
                    "searchTerm": movie_title,
                    "originalKeyword": movie_title  # 添加原始关键词用于识别词匹配
                }
                logger.info(f"🎯 使用关键词模式导入电影: {movie_title}")
            else:
                # 非识别词匹配时使用provider模式
                import_params = {
                    "searchType": provider_type,
                    "searchTerm": provider_id,
                    "originalKeyword": movie_title if movie_title else f"{provider_type.upper()} ID: {provider_id}"  # 添加原始关键词用于识别词匹配
                }
                logger.info(f"🚀 使用Provider模式导入电影: {provider_type.upper()} {provider_id}")
            
            response = call_danmaku_api('POST', '/import/auto', params=import_params)
            
            # 构建媒体信息用于回调通知
            media_info = {
                'Name': movie_title if movie_title else f"{provider_type.upper()} {provider_id}",
                'Type': 'Movie',
                'ProviderId': provider_id,
                'ProviderType': provider_type
            }
            
            # 如果有电影标题，添加到媒体信息中
            if movie_title:
                media_info['MovieTitle'] = movie_title
            
            if response and response.get('success'):
                # 从data字段中获取taskId
                data = response.get('data', {})
                task_id = data.get('taskId')
                logger.info(f"✅ 电影导入成功 ({provider_type.upper()}: {provider_id}), taskId: {task_id}")
                
                # 发送成功回调通知，传递taskId
                if task_id:
                    await self._send_callback_notification('import', media_info, 'success', task_ids=[task_id])
                else:
                    await self._send_callback_notification('import', media_info, 'success')
                
                # 库缓存刷新已移除，改为直接调用/library/search接口
            else:
                error_msg = response.get('message', '未知错误') if response else '请求失败'
                logger.error(f"❌ 电影导入失败 ({provider_type.upper()}: {provider_id}): {error_msg}")
                
                # 发送失败回调通知
                await self._send_callback_notification('import', media_info, 'failed', error_msg)
                
        except Exception as e:
            logger.error(f"❌ 导入电影时发生错误 ({provider_type.upper()}: {provider_id}): {e}", exc_info=True)
    
    async def _import_movie(self, tmdb_id: str):
        """导入单个电影 (兼容性方法)
        
        Args:
            tmdb_id: TMDB电影ID
        """
        await self._import_movie_by_provider(tmdb_id, 'tmdb')
    
    async def _refresh_movie(self, source_id: str, movie_title: str = None):
        """刷新电影数据
        
        Args:
            source_id: 源ID
            movie_title: 电影标题（可选）
        """
        try:
            logger.info(f"🔄 开始刷新电影 (源ID: {source_id})")
            
            # 先获取源的分集列表来获取episodeId
            episodes_response = call_danmaku_api('GET', f'/library/source/{source_id}/episodes')
            if not episodes_response or not episodes_response.get('success'):
                logger.error(f"❌ 获取电影分集列表失败 (源ID: {source_id})")
                return
            
            source_episodes = episodes_response.get('data', [])
            if not source_episodes:
                logger.warning(f"⚠️ 电影源暂无分集信息 (源ID: {source_id})")
                return
            
            # 电影默认只取第一个分集的ID去刷新
            first_episode = source_episodes[0]
            episode_id = first_episode.get('episodeId')
            fetched_at = first_episode.get('fetchedAt')
            
            if not episode_id:
                logger.error(f"❌ 未找到电影的episodeId (源ID: {source_id})")
                return
            
            # 检查时间段判断机制：入库时间是否早于24小时
            if fetched_at:
                try:
                    # 解析fetchedAt时间（ISO 8601格式）并转换为配置的时区
                    fetched_time = datetime.fromisoformat(fetched_at.replace('Z', '+00:00'))
                    fetched_time_local = fetched_time.astimezone(self.timezone)
                    current_time_local = datetime.now(self.timezone)
                    time_diff = current_time_local - fetched_time_local
                    
                    if time_diff < timedelta(hours=24):
                        logger.info(f"⏰ 电影入库时间在24小时内 ({time_diff}），跳过刷新 (源ID: {source_id}) [时区: {self.timezone}]")
                        return
                    else:
                        logger.info(f"⏰ 电影入库时间超过24小时 ({time_diff}），执行刷新 (源ID: {source_id}) [时区: {self.timezone}]")
                except Exception as e:
                    logger.warning(f"⚠️ 解析入库时间失败，继续执行刷新: {e}")
            else:
                logger.info(f"ℹ️ 未找到入库时间信息，继续执行刷新 (源ID: {source_id})")
            
            logger.info(f"🔄 刷新电影分集 (episodeId: {episode_id})")
            
            # 使用episodeId刷新电影
            response = call_danmaku_api(
                method="POST",
                endpoint=f"/library/episode/{episode_id}/refresh"
            )
            
            # 添加调试日志查看完整响应
            logger.info(f"🔍 电影刷新API响应: {response}")
            
            # 构建媒体信息用于回调通知
            media_info = {
                'Name': movie_title if movie_title else f"源ID {source_id}",
                'Type': 'Movie',
                'SourceId': source_id,
                'EpisodeId': episode_id
            }
            
            # 添加电影标题（如果有）
            if movie_title:
                media_info['MovieTitle'] = movie_title
            
            if response and response.get('success'):
                # 从data字段中获取taskId
                data = response.get('data', {})
                task_id = data.get('taskId')
                logger.info(f"📊 响应data字段: {data}")
                logger.info(f"✅ 电影刷新成功 (源ID: {source_id}), taskId: {task_id}")
                
                # 发送成功回调通知，如果有taskId则启动轮询
                if task_id:
                    await self._send_callback_notification('refresh', media_info, 'success', task_ids=[task_id])
                else:
                    await self._send_callback_notification('refresh', media_info, 'success')
            else:
                error_msg = response.get('message', '未知错误') if response else '请求失败'
                logger.error(f"❌ 电影刷新失败 (源ID: {source_id}): {error_msg}")
                
                # 发送失败回调通知
                await self._send_callback_notification('refresh', media_info, 'failed', error_msg)
                
        except Exception as e:
            logger.error(f"❌ 刷新电影时发生错误 (源ID: {source_id}): {e}", exc_info=True)
    
    async def _import_episodes_by_provider(self, provider_id: str, provider_type: str, season: int, episodes: list, series_name: str = None, identify_matched: bool = False):
        """根据provider类型导入指定集数
        
        Args:
            provider_id: Provider ID (TMDB/TVDB/IMDB/Douban/Bangumi)
            provider_type: Provider类型 ('tmdb', 'tvdb', 'imdb', 'douban', 'bangumi')
            season: 季度
            episodes: 集数列表
            series_name: 剧集名称（可选）
        """
        if not episodes:
            logger.warning(f"⚠️ 集数列表为空，跳过导入: {provider_type.upper()} {provider_id} S{season}")
            return
        
        # 根据provider类型设置搜索参数
        search_type_map = {
            'tmdb': 'tmdb',
            'tvdb': 'tvdb', 
            'imdb': 'imdb',
            'douban': 'douban',
            'bangumi': 'bangumi',
            'keyword': 'keyword'
        }
        
        search_type = search_type_map.get(provider_type.lower(), 'tmdb')
        
        # 获取详细信息进行验证（仅TMDB支持）
        max_episodes = 0
        try:
            if provider_type.lower() == 'tmdb':
                tmdb_info = get_tmdb_media_details(provider_id, 'tv_series')
                if tmdb_info:
                    logger.info(f"📺 准备导入剧集: {tmdb_info.get('name', 'Unknown')} ({tmdb_info.get('year', 'N/A')})")
                    
                    # 验证季度有效性
                    seasons = tmdb_info.get('seasons', [])
                    valid_season = None
                    for s in seasons:
                        if s.get('season_number') == season:
                            valid_season = s
                            break
                    
                    if not valid_season:
                        logger.error(f"❌ 无效的季度: S{season}，可用季度: {[s.get('season_number') for s in seasons]}")
                        return
                    
                    max_episodes = valid_season.get('episode_count', 0)
                    logger.info(f"📊 季度信息: S{season} 共{max_episodes}集")
                else:
                    logger.warning(f"⚠️ 无法获取TMDB详细信息: {provider_id}，继续尝试导入")
            else:
                logger.info(f"📺 准备导入剧集: {provider_type.upper()} {provider_id} S{season}")
        except Exception as e:
            logger.warning(f"⚠️ 验证{provider_type.upper()}信息时出错: {e}，继续尝试导入")
        
        success_count = 0
        failed_count = 0
        task_ids = []  # 收集成功导入的taskId
        
        try:
            for episode in episodes:
                if episode is None:
                    continue
                    
                # 确保episode是整数类型
                try:
                    episode_num = int(episode) if isinstance(episode, str) else episode
                    if episode_num <= 0:
                        logger.warning(f"⚠️ 跳过无效集数: {episode_num}")
                        continue
                except (ValueError, TypeError):
                    logger.warning(f"⚠️ 跳过无效集数格式: {episode}")
                    continue
                
                # 验证集数是否超出范围（仅TMDB支持）
                if provider_type.lower() == 'tmdb' and max_episodes > 0 and episode_num > max_episodes:
                    logger.warning(f"⚠️ 集数超出范围: S{season}E{episode_num} > {max_episodes}集，跳过")
                    continue
                
                # 构建导入参数
                if identify_matched and series_name:
                    # 识别词匹配时使用关键词模式
                    import_params = {
                        "searchType": "keyword",
                        "searchTerm": series_name,
                        "mediaType": "tv_series",
                        "season": season,
                        "episode": episode_num,
                        "originalKeyword": series_name  # 添加原始关键词用于识别词匹配
                    }
                    logger.info(f"🎯 使用关键词模式导入: {series_name} S{season:02d}E{episode_num:02d}")
                else:
                    # 非识别词匹配时使用provider模式
                    import_params = {
                        "searchType": search_type,
                        "searchTerm": provider_id,
                        "mediaType": "tv_series",
                        "season": season,
                        "episode": episode_num,
                        "originalKeyword": series_name if series_name else f"{provider_type.upper()} ID: {provider_id}"  # 添加原始关键词用于识别词匹配
                    }
                    logger.info(f"🚀 使用Provider模式导入: {provider_type.upper()} {provider_id} S{season:02d}E{episode_num:02d}")
                
                # 调用导入API
                try:
                    response = call_danmaku_api(
                        method="POST",
                        endpoint="/import/auto",
                        params=import_params
                    )
                    
                    # 添加详细的API响应日志
                    logger.info(f"🔍 /import/auto API响应: {response}")
                    
                    if response and response.get("success"):
                        success_count += 1
                        # 从data字段中获取taskId
                        data = response.get('data', {})
                        task_id = data.get('taskId')
                        logger.info(f"📊 响应data字段: {data}")
                        if task_id:
                            task_ids.append(task_id)
                        logger.info(f"✅ 导入成功: S{season:02d}E{episode_num:02d}, taskId: {task_id}")
                    else:
                        failed_count += 1
                        error_msg = response.get('message', '未知错误') if response else '请求失败'
                        logger.warning(f"⚠️ 导入失败: S{season:02d}E{episode_num:02d} - {error_msg}")
                        
                except Exception as api_error:
                    failed_count += 1
                    logger.error(f"❌ 导入API调用异常: S{season:02d}E{episode_num:02d} - {api_error}")
            
            # 输出导入统计
            total_episodes = success_count + failed_count
            if total_episodes > 0:
                logger.info(f"📊 导入完成: 成功 {success_count}/{total_episodes} 集")
                if failed_count > 0:
                    logger.warning(f"⚠️ {failed_count} 集导入失败，请检查日志")
                
                # 构建媒体信息用于回调通知
                media_info = {
                    'Name': series_name if series_name else f"{provider_type.upper()} {provider_id} S{season}",
                    'Type': 'Series',
                    'ProviderId': provider_id,
                    'ProviderType': provider_type,
                    'Season': season,
                    'Episodes': episodes,
                    'SuccessCount': success_count,
                    'FailedCount': failed_count,
                    'TotalCount': total_episodes
                }
                
                # 添加剧集名称（如果有）
                if series_name:
                    media_info['SeriesName'] = series_name
                
                # 发送回调通知
                if success_count > 0 and failed_count == 0:
                    # 全部成功
                    await self._send_callback_notification('import', media_info, 'success', task_ids=task_ids)
                elif success_count > 0 and failed_count > 0:
                    # 部分成功
                    await self._send_callback_notification('import', media_info, 'success', f"{failed_count} 集导入失败", task_ids=task_ids)
                else:
                    # 全部失败
                    await self._send_callback_notification('import', media_info, 'failed', "所有集数导入失败")
                
                # 库缓存刷新已移除，改为直接调用/library/search接口
                if success_count > 0:
                    logger.info("✅ 集数导入完成")
                    
        except Exception as e:
            logger.error(f"❌ 导入集数异常: {e}", exc_info=True)
    
    async def _import_episodes(self, tmdb_id: str, season: int, episodes: list, series_name: str = None):
        """导入指定集数（兼容性方法）
        
        Args:
            tmdb_id: TMDB ID
            season: 季度
            episodes: 集数列表
            series_name: 剧集名称（可选）
        """
        await self._import_episodes_by_provider(tmdb_id, 'tmdb', season, episodes, series_name)
    
     
    def _get_priority_provider_info(self, media_info: Dict[str, Any]) -> tuple:
        """
        获取优先级Provider信息 (tmdb > tvdb > imdb > douban > bangumi)
        
        Args:
            media_info: 已提取的媒体信息（包含provider ID）
            
        Returns:
            tuple: (provider_type, provider_id, search_type)
        """
        # 按优先级检查：tmdb > tvdb > imdb > douban > bangumi
        tmdb_id = media_info.get('tmdb_id')
        if tmdb_id:
            return 'tmdb', tmdb_id, 'tmdb'
            
        # 暂时取消tvdb
        # tvdb_id = media_info.get('tvdb_id')
        # if tvdb_id:
        #     return 'tvdb', tvdb_id, 'tvdb'
            
        imdb_id = media_info.get('imdb_id')
        if imdb_id:
            return 'imdb', imdb_id, 'imdb'
            
        douban_id = media_info.get('douban_id')
        if douban_id:
            return 'douban', douban_id, 'douban'
            
        bangumi_id = media_info.get('bangumi_id')
        if bangumi_id:
            return 'bangumi', bangumi_id, 'bangumi'
            
        return None, None, None
    
    async def _refresh_episodes(self, source_id: str, episodes: list, tmdb_id: Optional[str], season_num: int, series_name: Optional[str] = None, year: Optional[str] = None, identify_matched: bool = False):
        """刷新指定集数
        
        Args:
            source_id: 源ID
            episodes: 集数列表
            tmdb_id: TMDB ID（可选，为None时尝试通过TMDB搜索获取）
            season_num: 季度号
            series_name: 剧集名称（用于TMDB搜索）
            year: 年份（用于TMDB搜索）
            identify_matched: 是否为识别词匹配
        """
        try:
            # 先获取源的分集列表来获取episodeId
            episodes_response = call_danmaku_api('GET', f'/library/source/{source_id}/episodes')
            if not episodes_response or not episodes_response.get('success'):
                logger.error(f"❌ 获取分集列表失败: source_id={source_id}")
                return
            
            source_episodes = episodes_response.get('data', [])
            if not source_episodes:
                logger.warning(f"⚠️ 源暂无分集信息: source_id={source_id}")
                return
            
            # 创建集数索引到集信息的映射（包含episodeId和fetchedAt）
            episode_map = {}
            for ep in source_episodes:
                if ep.get('episodeId'):
                    episode_map[ep.get('episodeIndex')] = {
                        'episodeId': ep.get('episodeId'),
                        'fetchedAt': ep.get('fetchedAt')
                    }
            
            success_count = 0
            failed_count = 0
            skipped_count = 0
            task_ids = []  # 收集刷新操作的taskId
            
            for episode in episodes:
                episode_info = episode_map.get(episode)
                if not episode_info:
                    # 当集数不存在时，根据识别词匹配状态决定处理方式
                    if identify_matched:
                        # 识别词匹配时，直接使用keyword/auto导入该集
                        logger.info(f"🔍 未找到第{episode}集且识别词匹配，直接关键词导入第{episode}集: {series_name} S{season_num}E{episode:02d}")
                        await self._import_episodes_by_provider(None, 'keyword', season_num, [episode], series_name, identify_matched)
                    else:
                        # 非识别词匹配时，使用原有TMDB搜索逻辑
                        current_tmdb_id = tmdb_id
                        
                        # 如果没有TMDB ID，尝试通过剧集名称搜索获取
                        if not current_tmdb_id and series_name:
                            logger.info(f"🔍 未找到第{episode}集且缺少TMDB ID，尝试通过TMDB搜索: {series_name} ({year})")
                            tmdb_search_result = search_tv_series_by_name_year(series_name, year)
                            
                            if tmdb_search_result:
                                # 验证搜索结果是否匹配
                                if validate_tv_series_match(tmdb_search_result, series_name, year, season_num, episode):
                                    current_tmdb_id = tmdb_search_result.get('tmdb_id')
                                    logger.info(f"✅ TMDB搜索成功，找到匹配的剧集: {tmdb_search_result.get('name')} (ID: {current_tmdb_id})")
                                else:
                                    logger.warning(f"⚠️ TMDB搜索结果验证失败: {series_name}")
                            else:
                                logger.info(f"ℹ️ TMDB搜索未找到匹配结果: {series_name} ({year})")
                        
                        if current_tmdb_id:
                            logger.warning(f"⚠️ 未找到第{episode}集的episodeId，尝试导入")
                            # 当集数不存在且有TMDB ID时，尝试导入该集
                            await self._import_single_episode(current_tmdb_id, season_num, episode)
                        else:
                            logger.info(f"ℹ️ 未找到第{episode}集的episodeId且无法获取TMDB ID，跳过导入")
                    continue
                
                episode_id = episode_info['episodeId']
                fetched_at = episode_info['fetchedAt']
                
                # 检查时间段判断机制：入库时间是否早于24小时
                if fetched_at:
                    try:
                        # 解析fetchedAt时间（ISO 8601格式）并转换为配置的时区
                        fetched_time = datetime.fromisoformat(fetched_at.replace('Z', '+00:00'))
                        fetched_time_local = fetched_time.astimezone(self.timezone)
                        current_time_local = datetime.now(self.timezone)
                        time_diff = current_time_local - fetched_time_local
                        
                        if time_diff < timedelta(hours=24):
                            logger.info(f"⏰ 第{episode}集入库时间在24小时内 ({time_diff}），跳过刷新 [时区: {self.timezone}]")
                            skipped_count += 1
                            continue
                        else:
                            logger.info(f"⏰ 第{episode}集入库时间超过24小时 ({time_diff}），执行刷新 [时区: {self.timezone}]")
                    except Exception as e:
                        logger.warning(f"⚠️ 解析第{episode}集入库时间失败，继续执行刷新: {e}")
                else:
                    logger.info(f"ℹ️ 第{episode}集未找到入库时间信息，继续执行刷新")
                
                logger.info(f"🔄 刷新集数: E{episode:02d} (episodeId: {episode_id})")
                
                # 使用新的API端点刷新指定集数
                response = call_danmaku_api(
                    method="POST",
                    endpoint=f"/library/episode/{episode_id}/refresh"
                )
                
                if response and response.get("success"):
                    # 从data字段中获取taskId
                    data = response.get('data', {})
                    task_id = data.get('taskId')
                    if task_id:
                        task_ids.append(task_id)
                        logger.info(f"✅ 集数刷新成功: E{episode:02d}, taskId: {task_id}")
                    else:
                        logger.info(f"✅ 集数刷新成功: E{episode:02d}")
                    success_count += 1
                else:
                    logger.warning(f"⚠️ 集数刷新失败: E{episode:02d}")
                    failed_count += 1
            
            # 构建媒体信息用于回调通知
            total_episodes = len(episodes)
            processed_episodes = success_count + failed_count
            
            if processed_episodes > 0:
                media_info = {
                    'Name': series_name if series_name else f"源ID {source_id} S{season_num}",
                    'Type': 'Series',
                    'SourceId': source_id,
                    'Season': season_num,
                    'Episodes': episodes,
                    'SuccessCount': success_count,
                    'FailedCount': failed_count,
                    'SkippedCount': skipped_count,
                    'TotalCount': total_episodes
                }
                
                # 添加剧集名称和TMDB ID（如果有）
                if series_name:
                    media_info['SeriesName'] = series_name
                if tmdb_id:
                    media_info['TmdbId'] = tmdb_id
                if year:
                    media_info['Year'] = year
                
                # 发送回调通知
                if success_count > 0 and failed_count == 0:
                    # 全部成功
                    if task_ids:
                        await self._send_callback_notification('refresh', media_info, 'success', task_ids=task_ids)
                    else:
                        await self._send_callback_notification('refresh', media_info, 'success')
                elif success_count > 0 and failed_count > 0:
                    # 部分成功
                    if task_ids:
                        await self._send_callback_notification('refresh', media_info, 'success', f"{failed_count} 集刷新失败", task_ids=task_ids)
                    else:
                        await self._send_callback_notification('refresh', media_info, 'success', f"{failed_count} 集刷新失败")
                elif failed_count > 0:
                    # 全部失败
                    await self._send_callback_notification('refresh', media_info, 'failed', "所有集数刷新失败", task_ids=task_ids)
                
                logger.info(f"📊 刷新完成: 成功 {success_count}/{processed_episodes} 集，跳过 {skipped_count} 集")
                    
        except Exception as e:
            logger.error(f"❌ 刷新集数异常: {e}")
    
    async def _import_single_episode(self, tmdb_id: str, season_num: int, episode: int):
        """导入单个集数
        
        Args:
            tmdb_id: TMDB ID
            season_num: 季度号
            episode: 集数
        """
        try:
            # 构建导入参数
            import_params = {
                "searchType": "tmdb",
                "searchTerm": str(tmdb_id),
                "mediaType": "tv_series",
                "importMethod": "auto",
                "season": season_num,
                "episode": episode,
                "originalKeyword": f"TMDB ID: {tmdb_id}"  # 添加原始关键词用于识别词匹配
            }
            
            logger.info(f"🚀 开始导入单集: TMDB {tmdb_id} S{season_num:02d}E{episode:02d}")
            
            # 调用导入API
            response = call_danmaku_api(
                method="POST",
                endpoint="/import/auto",
                params=import_params
            )
            
            task_ids = []
            success_count = 0
            failed_count = 0
            
            if response and response.get("success"):
                logger.info(f"✅ 单集导入成功: S{season_num:02d}E{episode:02d}")
                success_count = 1
                # 从data字段中获取taskId
                data = response.get('data', {})
                task_id = data.get('taskId')
                if task_id:
                    task_ids.append(task_id)
            else:
                logger.info(f"ℹ️ 单集可能不存在或已导入: S{season_num:02d}E{episode:02d}")
                failed_count = 1
            
            # 获取剧集名称（用于通知）
            series_name = None
            try:
                tmdb_info = get_tmdb_media_details(tmdb_id, 'tv_series')
                if tmdb_info:
                    series_name = tmdb_info.get('name')
            except Exception as e:
                logger.warning(f"⚠️ 获取TMDB详细信息时出错: {e}")
            
            # 构建媒体信息用于回调通知
            media_info = {
                'Name': series_name if series_name else f"TMDB {tmdb_id} S{season_num}",
                'Type': 'Series',
                'ProviderId': tmdb_id,
                'ProviderType': 'tmdb',
                'Season': season_num,
                'Episodes': [episode],
                'SuccessCount': success_count,
                'FailedCount': failed_count,
                'TotalCount': 1
            }
            
            # 添加剧集名称（如果有）
            if series_name:
                media_info['SeriesName'] = series_name
            
            # 发送回调通知，无论成功还是失败都传递task_ids
            if success_count > 0:
                await self._send_callback_notification('import', media_info, 'success', task_ids=task_ids)
            else:
                await self._send_callback_notification('import', media_info, 'failed', "单集导入失败", task_ids=task_ids)
                
        except Exception as e:
            logger.error(f"❌ 导入单集异常: {e}")
    
    def _get_clean_media_name(self, media_info: Dict[str, Any]) -> str:
        """从emby通知信息中获取媒体名称
        
        优先使用从emby webhook提取的完整媒体信息
        
        Args:
            media_info: 从emby webhook提取的媒体信息字典
            
        Returns:
            str: 媒体名称
        """
        # 优先级顺序：title (完整标题) > SeriesName (剧集名) > series_name (剧集名) > original_title (原始标题) > Name (兼容旧格式)
        name = (
            media_info.get('title') or 
            media_info.get('SeriesName') or 
            media_info.get('series_name') or 
            media_info.get('original_title') or 
            media_info.get('Name', '未知')
        )
        
        return name.strip()
    
    async def _start_polling_if_needed(self, callback_bot=None):
        """启动轮询任务（如果尚未启动）
        
        Args:
            callback_bot: 已初始化的Bot实例（可选）
        """
            
        if not self._polling_active and (self._webhook_tasks or self._webhook_import_tasks):
            self._polling_active = True
            self._polling_task = asyncio.create_task(self._polling_loop(callback_bot))
            logger.info("🔄 启动taskId轮询任务")
    
    async def _polling_loop(self, callback_bot=None):
        """轮询循环，每5秒检查一次taskId状态"""
        try:
            while self._polling_active and (self._webhook_tasks or self._webhook_import_tasks):
                logger.info(f"🔄 开始轮询检查，当前有 {len(self._webhook_tasks)} 个webhook任务，{len(self._webhook_import_tasks)} 个入库任务")
                
                # 首先处理入库任务，获取真实的taskId
                completed_import_tasks = []
                timeout_import_tasks = []
                current_time = datetime.now(self.timezone)
                
                for import_task_id, import_task_info in list(self._webhook_import_tasks.items()):
                    original_webhook_task = import_task_info['webhook_task']
                    start_time = import_task_info['start_time']
                    timeout_hours = import_task_info['timeout_hours']
                    all_task_ids = import_task_info.get('all_task_ids', [import_task_id])
                    
                    # 检查是否超时（默认1小时）
                    elapsed_time = current_time - start_time
                    if elapsed_time > timedelta(hours=timeout_hours):
                        logger.warning(f"⏰ 入库任务 {import_task_id} 轮询超时（{elapsed_time}），自动取消")
                        timeout_import_tasks.append((import_task_id, original_webhook_task))
                        continue
                    
                    all_real_task_ids = []
                    all_tasks_completed = True
                    # 轮询所有入库任务的execution接口
                    for task_id in all_task_ids:
                        logger.info(f"🔍 轮询入库任务execution: {task_id} (已运行 {elapsed_time})")
                        real_task_ids = await self._poll_import_task_execution(task_id)
                        if real_task_ids:
                            all_real_task_ids.extend(real_task_ids)
                            logger.info(f"✅ 入库任务 {task_id} 获取到executionTaskIds: {real_task_ids}")
                        else:
                            logger.info(f"⏳ 入库任务 {task_id} 仍在处理中，继续等待")
                            all_tasks_completed = False
                            
                    # 只有当所有入库任务的execution接口都执行完毕并获取到真实的taskId后，才创建新的webhook任务
                    if all_tasks_completed and all_real_task_ids:
                        # 获取到所有executionTaskId，创建新的webhook任务
                        new_webhook_id = str(uuid.uuid4())
                        new_webhook_task = WebhookTask(
                            webhook_id=new_webhook_id,
                            operation_type=original_webhook_task.operation_type,
                            media_info=original_webhook_task.media_info.copy(),
                            message_id=original_webhook_task.message_id,
                            chat_id=original_webhook_task.chat_id
                        )
                        new_webhook_task.task_ids.extend(all_real_task_ids)
                        
                        # 将新任务添加到webhook任务队列
                        self._webhook_tasks[new_webhook_id] = new_webhook_task
                        logger.info(f"✅ 入库任务 {import_task_id} 解析完成，所有execution接口已执行完毕，创建新webhook任务 {new_webhook_id}，executionTaskIds: {all_real_task_ids}")
                        completed_import_tasks.append(import_task_id)
                    elif all_tasks_completed:
                        # 所有任务都已完成但没有获取到任何taskId
                        logger.warning(f"⚠️ 入库任务 {import_task_id} 所有execution接口已执行完毕，但未获取到任何taskId")
                        completed_import_tasks.append(import_task_id)
                    else:
                        logger.info(f"⏳ 入库任务 {import_task_id} 仍有任务在处理中，继续等待")
                
                # 处理超时任务
                for timeout_task_id, timeout_webhook_task in timeout_import_tasks:
                    try:
                        if callback_bot:
                            # 构建超时失败消息
                            media_info = timeout_webhook_task.media_info
                            media_name = self._get_clean_media_name(media_info)
                            media_type = "电影" if media_info.get('Type') == 'Movie' else "剧集"
                            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
                            
                            # 构建通知消息
                            message_lines = [
                                f"🎬 **Webhook 导入通知**",
                                f"",
                                f"📺 **媒体信息**",
                                f"• 名称: {media_name}",
                                f"• 类型: {media_type}",
                                f"• 操作: 导入",
                                f"• 状态: 🔄 入库中 → ❌ 失败",
                                f"• 时间: {timestamp}"
                            ]
                            
                            # 添加超时信息
                            message_lines.append(f"• 原因: 导入任务执行失败")
                            
                            message = "\n".join(message_lines)
                            
                            # 更新消息
                            await callback_bot.edit_message_text(
                                chat_id=timeout_webhook_task.chat_id,
                                message_id=timeout_webhook_task.message_id,
                                text=message,
                                parse_mode='Markdown'
                            )
                            
                            logger.info(f"📤 已发送超时失败通知: {timeout_task_id}")
                        else:
                            logger.warning(f"🤖 Bot实例未提供，无法发送超时失败通知: {timeout_task_id}")
                    except Exception as e:
                        logger.error(f"❌ 发送超时失败通知失败: {e}")
                    
                    completed_import_tasks.append(timeout_task_id)
                
                # 清理已完成和超时的入库任务
                for import_task_id in completed_import_tasks:
                    del self._webhook_import_tasks[import_task_id]
                    logger.info(f"🗑️ 清理入库任务: {import_task_id}")
                
                # 检查所有webhook任务
                completed_webhooks = []
                
                for webhook_id, webhook_task in list(self._webhook_tasks.items()):
                    if webhook_task.completed:
                        continue
                    
                    # 检查该webhook的所有taskId
                    for task_id in webhook_task.task_ids:
                        if task_id not in webhook_task.task_statuses:
                            logger.info(f"🔍 轮询taskId: {task_id}")
                            # 轮询该taskId的状态
                            task_data = await self._poll_task_status(task_id)
                            if task_data:
                                webhook_task.task_statuses[task_id] = task_data
                                task_status = task_data.get('status', 'unknown')
                                logger.info(f"✅ taskId {task_id} 状态更新: {task_status}")
                            else:
                                logger.info(f"⏳ taskId {task_id} 仍在执行中，继续等待")
                        
                    # 如果所有taskId都有了最终状态，标记为完成
                    if len(webhook_task.task_statuses) == len(webhook_task.task_ids):
                        webhook_task.completed = True
                        completed_webhooks.append(webhook_id)
                        logger.info(f"🎉 webhook任务 {webhook_id} 所有taskId已完成轮询")
                        
                        # 更新通知消息
                        await self._update_notification_message(webhook_task)
                
                # 清理已完成的webhook任务
                for webhook_id in completed_webhooks:
                    del self._webhook_tasks[webhook_id]
                    logger.info(f"🗑️ 清理已完成的webhook任务: {webhook_id}")
                
                # 如果没有待处理的任务，停止轮询
                if not self._webhook_tasks and not self._webhook_import_tasks:
                    self._polling_active = False
                    logger.info("⏹️ 所有任务已完成，停止轮询")
                    break
                
                # 等待5秒后继续下一轮轮询
                await asyncio.sleep(5)
                
        except Exception as e:
            logger.error(f"❌ 轮询任务异常: {e}")
            # 异常情况下清理所有任务状态，避免资源泄漏
            try:
                logger.warning("🧹 异常情况下清理任务状态")
                self._webhook_tasks.clear()
                self._webhook_import_tasks.clear()
                logger.info("✅ 任务状态清理完成")
            except Exception as cleanup_error:
                logger.error(f"❌ 清理任务状态失败: {cleanup_error}")
            finally:
                self._polling_active = False
    
    async def _poll_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """轮询单个taskId的状态
        
        Args:
            task_id: 要轮询的taskId
            
        Returns:
            task data dict if completed, None if still running
        """
        try:
            logger.debug(f"🔍 开始轮询taskId: {task_id}")
            # 在线程池中运行同步API调用，避免阻塞事件循环
            response = await asyncio.to_thread(
                call_danmaku_api,
                method="GET",
                endpoint=f"/tasks/{task_id}"
            )
            
            logger.debug(f"📡 API响应: {response}")
            
            if response and response.get("success"):
                # 新的返回结构: { "data": { "taskId": "string", "title": "string", "status": "string", "progress": 0, "description": "string", "createdAt": "2025-09-19T15:33:29.845Z" } }
                data = response.get('data', {})
                task_status = data.get('status')
                task_progress = data.get('progress', 0)
                task_title = data.get('title', '')
                
                logger.info(f"📊 任务状态: {task_status}, 进度: {task_progress}%, 标题: {task_title}")
                logger.debug(f"📋 完整data字段: {data}")
                
                # 检查任务是否完成（根据实际API返回的状态值调整）
                if task_status in ['completed', 'finished', 'success', '已完成', '完成', '成功', '已成功']:
                    logger.info(f"✅ taskId {task_id} 已完成")
                    return data  # 返回完整的任务数据
                elif task_status in ['failed', 'error', '失败', '已失败']:
                    logger.warning(f"❌ taskId {task_id} 执行失败")
                    return data  # 返回完整的任务数据
                else:
                    logger.debug(f"⏳ taskId {task_id} 仍在执行中，状态: {task_status}")
                    return None
            elif response and response.get("status_code") == 404:
                # 任务还未准备好，继续等待
                logger.debug(f"⏳ taskId {task_id} 返回404，任务尚未准备好")
                return None
            else:
                logger.warning(f"⚠️ 轮询taskId {task_id} 失败: {response}")
                        
        except Exception as e:
            logger.error(f"❌ 轮询taskId {task_id} 失败: {e}")
        
        return None
    
    async def _poll_import_task_execution(self, import_task_id: str) -> Optional[List[str]]:
        """轮询入库任务的execution接口获取真实的taskId列表
        
        Args:
            import_task_id: 入库操作返回的taskId
            
        Returns:
            List of real taskIds if available, None if still processing
        """
        try:
            logger.debug(f"🔍 开始轮询入库任务execution: {import_task_id}")
            # 调用/tasks/{taskId}/execution接口
            response = await asyncio.to_thread(
                call_danmaku_api,
                method="GET",
                endpoint=f"/tasks/{import_task_id}/execution"
            )
            
            logger.debug(f"📡 入库任务execution API响应: {response}")
            
            if response and response.get("success"):
                data = response.get('data', {})
                task_ids = []
                
                if isinstance(data, dict):
                    # 检查是否有多任务ID字段
                    if 'executionTaskIds' in data and isinstance(data['executionTaskIds'], list):
                        # 如果有多个executionTaskId
                        task_ids.extend(data['executionTaskIds'])
                    elif 'tasks' in data and isinstance(data['tasks'], list):
                        # 检查是否有tasks列表
                        for task in data['tasks']:
                            if isinstance(task, dict):
                                task_id = task.get('taskId', task.get('id'))
                                if task_id:
                                    task_ids.append(task_id)
                    else:
                        # 检查单个taskId字段
                        execution_task_id = data.get('executionTaskId')
                        if not execution_task_id:
                            execution_task_id = data.get('taskId')
                            if not execution_task_id:
                                execution_task_id = data.get('id')
                        if execution_task_id:
                            task_ids.append(execution_task_id)
                elif isinstance(data, str):
                    # 如果data直接是taskId字符串
                    task_ids.append(data)
                elif isinstance(data, list):
                    # 如果data直接是taskIds列表
                    task_ids.extend(data)
                
                if task_ids:
                    logger.info(f"✅ 入库任务 {import_task_id} 获取到taskIds: {task_ids}")
                    # 确保所有taskId都是字符串
                    return [str(task_id) for task_id in task_ids]
                else:
                    logger.debug(f"⏳ 入库任务 {import_task_id} 尚未生成executionTaskId")
                    return None
            elif response and response.get("status_code") == 404:
                # 任务还未准备好，继续等待
                logger.debug(f"⏳ 入库任务 {import_task_id} 返回404，任务尚未准备好")
                return None
            else:
                logger.warning(f"⚠️ 轮询入库任务execution {import_task_id} 失败: {response}")
                        
        except Exception as e:
            logger.error(f"❌ 轮询入库任务execution {import_task_id} 失败: {e}")
        
        return None
    
    async def _update_notification_message(self, webhook_task: WebhookTask):
        """更新通知消息，添加状态信息
        
        Args:
            webhook_task: webhook任务信息
        """
        try:
            # 使用现有的TELEGRAM_BOT_TOKEN创建Bot实例
            callback_bot = Bot(token=self.config.telegram.bot_token)
            
            # 构建更新后的消息
            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
            
            # 获取媒体基本信息
            media_info = webhook_task.media_info
            media_name = self._get_clean_media_name(media_info)
            media_type = "电影" if media_info.get('Type') == 'Movie' else "剧集"
            
            # 构建操作类型描述
            base_operation_text = "导入" if webhook_task.operation_type == "import" else "刷新"
            
            # 为剧集构建包含季集信息的操作描述
            operation_text = base_operation_text
            if media_info.get('Type') == 'Series':
                season = media_info.get('Season')
                episodes = media_info.get('Episodes', [])
                
                if season and episodes:
                    # 构建季集信息字符串
                    episode_list = []
                    for ep in episodes:
                        episode_list.append(f"S{season}E{ep:02d}")
                    
                    if episode_list:
                        operation_text = f"{base_operation_text}{','.join(episode_list)}"
            
            # 构建通知消息
            message_lines = [
                f"🎬 **Webhook {base_operation_text}通知**",
                f"",
                f"📺 **媒体信息**",
                f"• 名称: {media_name}",
                f"• 类型: {media_type}",
                f"• 操作: {operation_text}",
                f"• 状态: 🔄 刷新中 → ✅ 处理完成" if webhook_task.operation_type == "refresh" else (f"• 状态: 🔄 入库中 → ✅ 处理完成" if webhook_task.operation_type == "import" else f"• 状态: ✅ 成功 → ✅ 处理完成"),
                f"• 时间: {timestamp}"
            ]
            
            # 添加剧集特有信息
            if media_info.get('Type') == 'Series':
                if media_info.get('Season'):
                    message_lines.insert(-1, f"• 季度: S{media_info.get('Season')}")
                
                # 添加统计信息
                success_count = media_info.get('SuccessCount', 0)
                failed_count = media_info.get('FailedCount', 0)
                total_count = media_info.get('TotalCount', 0)
                skipped_count = media_info.get('SkippedCount', 0)
                
                if total_count > 0:
                    stats_parts = []
                    if success_count > 0:
                        stats_parts.append(f"成功{success_count}集")
                    if failed_count > 0:
                        stats_parts.append(f"失败{failed_count}集")
                    if skipped_count > 0:
                        stats_parts.append(f"跳过{skipped_count}集")
                    
                    if stats_parts:
                        message_lines.insert(-1, f"• 统计: {' / '.join(stats_parts)} (共{total_count}集)")
            
            # 添加Provider信息
            if media_info.get('ProviderType') and media_info.get('ProviderId'):
                message_lines.insert(-1, f"• Provider: {media_info.get('ProviderType').upper()} {media_info.get('ProviderId')}")
            elif media_info.get('SourceId'):
                message_lines.insert(-1, f"• 源ID: {media_info.get('SourceId')}")
            
            # 添加任务执行信息
            message_lines.extend([
                f"",
                f"⚙️ **任务执行信息**"
            ])
            
            # 检查是否有失败的任务
            has_failed_tasks = any(
                isinstance(task_data, dict) and task_data.get('status') in ['failed', 'error', '失败', '已失败']
                or isinstance(task_data, str) and task_data in ['failed', 'error', '失败', '已失败']
                for task_data in webhook_task.task_statuses.values()
            )
            
            # 显示所有任务的详细信息
            for task_id, task_data in webhook_task.task_statuses.items():
                if isinstance(task_data, dict):
                    status = task_data.get('status', 'unknown')
                    description = task_data.get('description', '')
                    progress = task_data.get('progress', 0)
                    
                    # 为不同状态添加视觉指示
                    status_icon = "✅" if status in ['completed', 'finished', 'success', '已完成', '完成', '成功', '已成功'] else "❌" if status in ['failed', 'error', '失败', '已失败'] else "🔄"
                    status_text = f"{status_icon} {status}"
                    
                    # 显示任务ID
                    message_lines.append(f"• TaskID: `{task_id}`")
                    # 显示状态和进度
                    message_lines.append(f"  └─ 状态: {status_text} ({progress}%)" if progress > 0 else f"  └─ 状态: {status_text}")
                    
                    # 显示描述信息（如错误详情）
                    if description:
                        # 处理多行描述
                        description_lines = description.split('\n')
                        for line in description_lines:
                            if line.strip():
                                message_lines.append(f"  └─ 📋 {line.strip()}")
                else:
                    # 兼容旧格式（字符串状态）
                    status = str(task_data)
                    status_icon = "✅" if status in ['completed', 'finished', 'success', '已完成', '完成', '成功', '已成功'] else "❌" if status in ['failed', 'error', '失败', '已失败'] else "🔄"
                    status_text = f"{status_icon} {status}"
                    
                    message_lines.append(f"• TaskID: `{task_id}`")
                    message_lines.append(f"  └─ 状态: {status_text}")
            
            # 如果没有失败任务但有任务状态，以简洁方式显示所有任务状态
            if not has_failed_tasks and webhook_task.task_statuses:
                # 这里逻辑已合并到上方的统一处理中
                pass
            
            if media_info.get('Overview'):
                overview = media_info.get('Overview', '')[:100]
                if len(media_info.get('Overview', '')) > 100:
                    overview += "..."
                message_lines.extend([
                    f"",
                    f"📝 **简介**",
                    f"{overview}"
                ])
            
            message = "\n".join(message_lines)
            
            # 更新消息
            await callback_bot.edit_message_text(
                chat_id=webhook_task.chat_id,
                message_id=webhook_task.message_id,
                text=message,
                parse_mode='Markdown'
            )
            
            logger.info(f"📝 更新通知消息成功: {operation_text} {media_name}")
            
        except Exception as e:
            logger.error(f"❌ 更新通知消息失败: {e}")
    
    async def _send_callback_notification(self, operation_type: str, media_info: Dict[str, Any], result: str = "success", error_msg: str = None, task_ids: List[str] = None):
        """发送回调通知
        
        Args:
            operation_type: 操作类型 (import/refresh)
            media_info: 媒体信息
            result: 操作结果 (success/failed)
            error_msg: 错误信息（可选）
            task_ids: 任务ID列表（可选，用于轮询状态）
        """
        try:
            # 检查回调通知是否启用
            if not self.config.webhook.callback_enabled:
                return
            
            # 发送所有状态的通知
            # 不再限制只发送成功的通知，让失败的任务也能被正确记录和追踪
            
            # 检查配置是否有效
            if not self.config.webhook.callback_chat_id:
                logger.warning("⚠️ 回调通知聊天ID未配置，跳过发送")
                return
            
            # 使用现有的TELEGRAM_BOT_TOKEN创建Bot实例
            callback_bot = Bot(token=self.config.telegram.bot_token)
            
            # 构建通知消息
            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
            
            # 获取媒体基本信息
            # 优先使用TMDB或library匹配的名称
            media_name = self._get_clean_media_name(media_info)
            media_type = "电影" if media_info.get('Type') == 'Movie' else "剧集"
            
            # 构建状态图标和描述
            if result == "success":
                if operation_type == "refresh":
                    status_icon = "🔄"
                    status_text = "刷新中"
                elif operation_type == "import":
                    status_icon = "🔄"
                    status_text = "入库中"
                else:
                    status_icon = "✅"
                    status_text = "成功"
            else:
                status_icon = "❌"
                status_text = "失败"
            
            # 构建操作类型描述
            base_operation_text = "导入" if operation_type == "import" else "刷新"
            
            # 为剧集构建包含季集信息的操作描述
            operation_text = base_operation_text
            if media_info.get('Type') == 'Series':
                season = media_info.get('Season')
                episodes = media_info.get('Episodes', [])
                
                if season and episodes:
                    # 构建季集信息字符串
                    episode_list = []
                    for ep in episodes:
                        episode_list.append(f"S{season}E{ep:02d}")
                    
                    if episode_list:
                        operation_text = f"{base_operation_text}{','.join(episode_list)}"
            
            # 构建通知消息
            message_lines = [
                f"🎬 **Webhook {base_operation_text}通知**",
                f"",
                f"📺 **媒体信息**",
                f"• 名称: {media_name}",
                f"• 类型: {media_type}",
                f"• 操作: {operation_text}",
                f"• 状态: {status_icon} {status_text}",
                f"• 时间: {timestamp}"
            ]
            
            # 添加剧集特有信息
            if media_info.get('Type') == 'Series':
                if media_info.get('Season'):
                    message_lines.insert(-1, f"• 季度: S{media_info.get('Season')}")
                
                # 添加统计信息
                success_count = media_info.get('SuccessCount', 0)
                failed_count = media_info.get('FailedCount', 0)
                total_count = media_info.get('TotalCount', 0)
                skipped_count = media_info.get('SkippedCount', 0)
                
                if total_count > 0:
                    stats_parts = []
                    if success_count > 0:
                        stats_parts.append(f"成功{success_count}集")
                    if failed_count > 0:
                        stats_parts.append(f"失败{failed_count}集")
                    if skipped_count > 0:
                        stats_parts.append(f"跳过{skipped_count}集")
                    
                    if stats_parts:
                        message_lines.insert(-1, f"• 统计: {' / '.join(stats_parts)} (共{total_count}集)")
            
            # 添加Provider信息
            if media_info.get('ProviderType') and media_info.get('ProviderId'):
                message_lines.insert(-1, f"• Provider: {media_info.get('ProviderType').upper()} {media_info.get('ProviderId')}")
            elif media_info.get('SourceId'):
                message_lines.insert(-1, f"• 源ID: {media_info.get('SourceId')}")
            
            # 如果有错误信息，添加到消息中
            if error_msg:
                message_lines.extend([
                    f"",
                    f"❌ **错误信息**",
                    f"```",
                    f"{error_msg}",
                    f"```"
                ])
            
            if media_info.get('Overview'):
                overview = media_info.get('Overview', '')[:100]
                if len(media_info.get('Overview', '')) > 100:
                    overview += "..."
                message_lines.extend([
                    f"",
                    f"📝 **简介**",
                    f"{overview}"
                ])
            
            message = "\n".join(message_lines)
            
            # 发送通知
            sent_message = await callback_bot.send_message(
                chat_id=self.config.webhook.callback_chat_id,
                text=message,
                parse_mode='Markdown'
            )
            
            # 如果有taskIds，记录webhook任务用于后续轮询
            if task_ids and sent_message:
                webhook_id = str(uuid.uuid4())
                webhook_task = WebhookTask(
                    webhook_id=webhook_id,
                    operation_type=operation_type,
                    media_info=media_info.copy(),
                    message_id=sent_message.message_id,
                    chat_id=self.config.webhook.callback_chat_id
                )
                
                if operation_type == "import":
                    # 入库操作：将所有taskIds合并到一个入库任务中处理，避免消息覆盖
                    # 对于多集导入，我们使用第一个task_id作为键，但保留所有task_ids的信息
                    if task_ids:
                        # 使用第一个task_id作为键
                        main_task_id = task_ids[0]
                        self._webhook_import_tasks[main_task_id] = {
                            'webhook_task': webhook_task,
                            'start_time': datetime.now(self.timezone),
                            'timeout_hours': 1,
                            'all_task_ids': task_ids  # 保存所有task_ids
                        }
                        logger.info(f"📝 记录入库任务: {webhook_id}, 待解析taskIds: {task_ids}")
                    # 入库任务不立即添加到_webhook_tasks，等获取executionTaskId后再创建新任务
                else:
                    # 刷新操作：taskIds可以直接轮询
                    webhook_task.task_ids.extend(task_ids)
                    logger.info(f"📝 记录刷新任务: {webhook_id}, taskIds: {task_ids}")
                    self._webhook_tasks[webhook_id] = webhook_task
                
                # 启动轮询任务（如果尚未启动），并传递已创建的callback_bot实例
                await self._start_polling_if_needed(callback_bot)
            
            logger.info(f"📤 回调通知发送成功: {operation_text} {media_name}")
            
        except Exception as e:
            logger.error(f"❌ 发送回调通知失败: {e}")


# 全局webhook处理器实例
webhook_handler = WebhookHandler()


def set_bot_instance(bot: Bot):
    """设置Bot实例
    
    Args:
        bot: Telegram Bot实例
    """
    global webhook_handler
    webhook_handler.bot = bot
    logger.info("🔌 Webhook handler bot instance set")
