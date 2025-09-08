import requests
import logging
from typing import Optional, List, Dict, Any
from config import TMDB_API_KEY, TMDB_BASE_URL, TMDB_ENABLED

logger = logging.getLogger(__name__)

def validate_tmdb_api_key(api_key: str) -> bool:
    """验证TMDB API密钥是否有效
    
    Args:
        api_key: TMDB API密钥
        
    Returns:
        bool: 密钥是否有效
    """
    if not api_key or not api_key.strip():
        return False
        
    try:
        # 使用配置API来验证密钥
        url = f"{TMDB_BASE_URL}/configuration"
        params = {'api_key': api_key}
        
        response = requests.get(url, params=params, timeout=10)
        
        # 如果返回200且有有效的JSON响应，说明密钥有效
        if response.status_code == 200:
            data = response.json()
            # 检查是否包含预期的配置字段
            return 'images' in data and 'base_url' in data.get('images', {})
        else:
            logger.debug(f"TMDB API密钥验证失败: HTTP {response.status_code}")
            return False
            
    except Exception as e:
        logger.debug(f"TMDB API密钥验证异常: {e}")
        return False

class TMDBSearchResult:
    """TMDB搜索结果封装类"""
    
    def __init__(self, results: List[Dict[str, Any]]):
        self.results = results
        self.movies = [r for r in results if r.get('media_type') == 'movie']
        self.tv_shows = [r for r in results if r.get('media_type') == 'tv']
    
    @property
    def total_count(self) -> int:
        """总结果数量"""
        return len(self.results)
    
    @property
    def movie_count(self) -> int:
        """电影数量"""
        return len(self.movies)
    
    @property
    def tv_count(self) -> int:
        """电视剧数量"""
        return len(self.tv_shows)
    
    @property
    def has_single_type(self) -> bool:
        """是否只有单一类型"""
        return (self.movie_count > 0) != (self.tv_count > 0)
    
    @property
    def dominant_type(self) -> Optional[str]:
        """主导类型（如果只有一种类型或某种类型占绝对优势）"""
        if self.movie_count > 0 and self.tv_count == 0:
            return 'movie'
        elif self.tv_count > 0 and self.movie_count == 0:
            return 'tv_series'
        else:
            return None  # 类型混合，需要用户选择
    
    def get_best_match(self) -> Optional[Dict[str, Any]]:
        """获取最佳匹配结果（按受欢迎度排序的第一个）"""
        if not self.results:
            return None
        
        # 按受欢迎度排序
        sorted_results = sorted(
            self.results, 
            key=lambda x: x.get('popularity', 0), 
            reverse=True
        )
        return sorted_results[0]


def search_tmdb_multi(query: str, language: str = 'zh-CN') -> Optional[TMDBSearchResult]:
    """使用TMDB多媒体搜索API搜索内容
    
    Args:
        query: 搜索关键词
        language: 语言代码，默认中文
        
    Returns:
        TMDBSearchResult对象，如果搜索失败返回None
    """
    if not TMDB_ENABLED:
        logger.debug("TMDB API未启用，跳过搜索")
        return None
    
    try:
        url = f"{TMDB_BASE_URL}/search/multi"
        params = {
            'api_key': TMDB_API_KEY,
            'query': query,
            'language': language,
            'page': 1
        }
        
        logger.info(f"🔍 调用TMDB搜索API: {query}")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        results = data.get('results', [])
        
        # 过滤掉人物结果，只保留电影和电视剧
        media_results = [
            r for r in results 
            if r.get('media_type') in ['movie', 'tv']
        ]
        
        logger.info(f"✅ TMDB搜索完成，找到 {len(media_results)} 个媒体结果")
        return TMDBSearchResult(media_results)
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ TMDB API请求失败: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ TMDB搜索处理失败: {e}")
        return None


def get_media_type_suggestion(query: str) -> Optional[str]:
    """根据TMDB搜索结果建议媒体类型
    
    Args:
        query: 搜索关键词
        
    Returns:
        建议的媒体类型: 'movie', 'tv_series' 或 None（需要用户选择）
    """
    search_result = search_tmdb_multi(query)
    
    if not search_result or search_result.total_count == 0:
        logger.info(f"📝 TMDB未找到结果，使用默认流程")
        return None
    
    # 记录搜索结果统计
    logger.info(
        f"📊 TMDB搜索统计 - 总计: {search_result.total_count}, "
        f"电影: {search_result.movie_count}, 电视剧: {search_result.tv_count}"
    )
    
    # 获取主导类型
    dominant_type = search_result.dominant_type
    
    if dominant_type:
        best_match = search_result.get_best_match()
        title = best_match.get('title') or best_match.get('name', '未知')
        type_name = '电影' if dominant_type == 'movie' else '电视剧'
        logger.info(f"🎯 TMDB建议类型: {type_name} (最佳匹配: {title})")
        return dominant_type
    else:
        logger.info(f"🤔 TMDB结果类型混合，需要用户手动选择")
        return None


def format_tmdb_results_info(query: str) -> str:
    """格式化TMDB搜索结果信息用于显示
    
    Args:
        query: 搜索关键词
        
    Returns:
        格式化的结果信息字符串
    """
    search_result = search_tmdb_multi(query)
    
    if not search_result or search_result.total_count == 0:
        return "🔍 TMDB未找到相关结果"
    
    info_parts = []
    info_parts.append(f"🎬 TMDB找到 {search_result.total_count} 个结果")
    
    if search_result.movie_count > 0:
        info_parts.append(f"电影: {search_result.movie_count}个")
    
    if search_result.tv_count > 0:
        info_parts.append(f"电视剧: {search_result.tv_count}个")
    
    # 显示最佳匹配
    best_match = search_result.get_best_match()
    if best_match:
        title = best_match.get('title') or best_match.get('name', '未知')
        media_type = '电影' if best_match.get('media_type') == 'movie' else '电视剧'
        year = best_match.get('release_date', best_match.get('first_air_date', ''))[:4] if best_match.get('release_date') or best_match.get('first_air_date') else ''
        year_info = f" ({year})" if year else ""
        info_parts.append(f"最佳匹配: {title}{year_info} [{media_type}]")
    
    return "\n".join(info_parts)


def search_tv_series_by_name_year(series_name: str, year: Optional[str] = None, language: str = 'zh-CN') -> Optional[Dict[str, Any]]:
    """通过剧集名称和年份搜索电视剧，返回最佳匹配的TMDB ID和详细信息
    
    Args:
        series_name: 剧集名称
        year: 年份（可选）
        language: 语言代码，默认中文
        
    Returns:
        包含TMDB ID和详细信息的字典，如果未找到匹配返回None
        返回格式: {
            'tmdb_id': str,
            'name': str,
            'original_name': str,
            'first_air_date': str,
            'year': str,
            'overview': str,
            'vote_average': float,
            'number_of_seasons': int,
            'number_of_episodes': int
        }
    """
    if not TMDB_ENABLED:
        logger.debug("TMDB API未启用，跳过电视剧搜索")
        return None
    
    try:
        url = f"{TMDB_BASE_URL}/search/tv"
        params = {
            'api_key': TMDB_API_KEY,
            'query': series_name,
            'language': language,
            'page': 1
        }
        
        # 如果提供了年份，添加到搜索参数中
        if year:
            params['first_air_date_year'] = year
        
        logger.info(f"🔍 通过TMDB搜索电视剧: {series_name}" + (f" ({year})" if year else ""))
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        results = data.get('results', [])
        
        if not results:
            logger.info(f"❌ TMDB未找到匹配的电视剧: {series_name}")
            return None
        
        # 寻找最佳匹配
        best_match = None
        best_score = 0
        
        for result in results:
            score = 0
            result_name = result.get('name', '')
            result_original_name = result.get('original_name', '')
            result_first_air_date = result.get('first_air_date', '')
            result_year = result_first_air_date[:4] if result_first_air_date else ''
            
            # 名称匹配评分
            if series_name.lower() in result_name.lower() or result_name.lower() in series_name.lower():
                score += 50
            if series_name.lower() in result_original_name.lower() or result_original_name.lower() in series_name.lower():
                score += 30
            
            # 年份匹配评分
            if year and result_year == year:
                score += 40
            elif year and result_year and abs(int(year) - int(result_year)) <= 1:
                score += 20  # 允许1年误差
            
            # 受欢迎度加分
            popularity = result.get('popularity', 0)
            score += min(popularity / 10, 10)  # 最多加10分
            
            logger.debug(f"📊 匹配评分: {result_name} ({result_year}) - {score}分")
            
            if score > best_score:
                best_score = score
                best_match = result
        
        if not best_match or best_score < 30:  # 设置最低匹配分数阈值
            logger.info(f"❌ TMDB未找到足够匹配的电视剧: {series_name} (最高分数: {best_score})")
            return None
        
        # 格式化返回结果
        tmdb_id = str(best_match.get('id', ''))
        result_info = {
            'tmdb_id': tmdb_id,
            'name': best_match.get('name', ''),
            'original_name': best_match.get('original_name', ''),
            'first_air_date': best_match.get('first_air_date', ''),
            'year': best_match.get('first_air_date', '')[:4] if best_match.get('first_air_date') else '',
            'overview': best_match.get('overview', ''),
            'vote_average': best_match.get('vote_average', 0),
            'popularity': best_match.get('popularity', 0)
        }
        
        # 获取详细信息以获取季数和集数
        detailed_info = get_tmdb_media_details(tmdb_id, 'tv_series', language)
        if detailed_info:
            result_info['number_of_seasons'] = detailed_info.get('number_of_seasons', 0)
            result_info['number_of_episodes'] = detailed_info.get('number_of_episodes', 0)
        
        logger.info(f"✅ TMDB找到匹配的电视剧: {result_info['name']} ({result_info['year']}) - ID: {tmdb_id} (匹配分数: {best_score})")
        return result_info
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ TMDB电视剧搜索API请求失败: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ TMDB电视剧搜索处理失败: {e}")
        return None


def validate_tv_series_match(tmdb_info: Dict[str, Any], series_name: str, year: Optional[str] = None, 
                           season_number: Optional[int] = None, episode_number: Optional[int] = None) -> bool:
    """验证TMDB搜索结果是否与剧集信息匹配
    
    Args:
        tmdb_info: TMDB搜索返回的剧集信息
        series_name: 原始剧集名称
        year: 年份（可选）
        season_number: 季数（可选）
        episode_number: 集数（可选）
        
    Returns:
        bool: 是否匹配
    """
    if not tmdb_info:
        return False
    
    try:
        # 验证名称匹配
        tmdb_name = tmdb_info.get('name', '')
        tmdb_original_name = tmdb_info.get('original_name', '')
        
        name_match = (
            series_name.lower() in tmdb_name.lower() or tmdb_name.lower() in series_name.lower() or
            series_name.lower() in tmdb_original_name.lower() or tmdb_original_name.lower() in series_name.lower()
        )
        
        if not name_match:
            logger.debug(f"❌ 名称不匹配: {series_name} vs {tmdb_name}")
            return False
        
        # 验证年份匹配（允许1年误差）
        if year:
            tmdb_year = tmdb_info.get('year', '')
            if tmdb_year and abs(int(year) - int(tmdb_year)) > 1:
                logger.debug(f"❌ 年份不匹配: {year} vs {tmdb_year}")
                return False
        
        # 验证季数匹配
        if season_number:
            tmdb_seasons = tmdb_info.get('number_of_seasons', 0)
            if tmdb_seasons > 0 and season_number > tmdb_seasons:
                logger.debug(f"❌ 季数超出范围: S{season_number} > {tmdb_seasons}季")
                return False
        
        logger.info(f"✅ TMDB匹配验证通过: {tmdb_name} ({tmdb_info.get('year', '')})")
        return True
        
    except Exception as e:
        logger.error(f"❌ TMDB匹配验证失败: {e}")
        return False


def get_tmdb_media_details(tmdb_id: str, media_type: str, language: str = 'zh-CN') -> Optional[Dict[str, Any]]:
    """获取TMDB媒体详细信息
    
    Args:
        tmdb_id: TMDB媒体ID
        media_type: 媒体类型，'movie' 或 'tv_series'
        language: 语言代码，默认中文
        
    Returns:
        包含媒体详细信息的字典，如果获取失败返回None
        对于电视剧，会包含seasons信息，避免额外的API调用
    """
    if not TMDB_ENABLED:
        logger.debug("TMDB API未启用，跳过获取详细信息")
        return None
    
    try:
        # 转换媒体类型
        api_media_type = 'tv' if media_type == 'tv_series' else 'movie'
        
        url = f"{TMDB_BASE_URL}/{api_media_type}/{tmdb_id}"
        params = {
            'api_key': TMDB_API_KEY,
            'language': language
        }
        
        logger.info(f"🔍 获取TMDB媒体详细信息: ID={tmdb_id}, 类型={media_type}")
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        # 对于电视剧，直接从详情API获取季度信息，避免额外调用
        if media_type == 'tv_series' and 'seasons' in data:
            seasons = data.get('seasons', [])
            # 过滤掉特殊季度（如第0季）并格式化
            valid_seasons = []
            for season in seasons:
                season_number = season.get('season_number', 0)
                if season_number > 0:  # 只保留正常季度
                    valid_seasons.append({
                        'season_number': season_number,
                        'name': season.get('name', f'第{season_number}季'),
                        'episode_count': season.get('episode_count', 0),
                        'air_date': season.get('air_date', ''),
                        'overview': season.get('overview', '')
                    })
            # 将处理后的季度信息添加到返回数据中
            data['processed_seasons'] = valid_seasons
            logger.info(f"✅ TMDB电视剧详细信息获取成功，包含{len(valid_seasons)}季信息")
        else:
            logger.info(f"✅ TMDB媒体详细信息获取成功")
            
        return data
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ TMDB API请求失败: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ TMDB媒体详细信息获取失败: {e}")
        return None


def get_tmdb_tv_seasons(tmdb_id: str, language: str = 'zh-CN') -> Optional[List[Dict[str, Any]]]:
    """获取TMDB电视剧的季度信息
    
    Args:
        tmdb_id: TMDB电视剧ID
        language: 语言代码，默认中文
        
    Returns:
        季度信息列表，每个季度包含season_number、name、episode_count等信息
        如果获取失败返回None
    """
    if not TMDB_ENABLED:
        logger.debug("TMDB API未启用，跳过获取季度信息")
        return None
    
    try:
        # 优化：直接使用get_tmdb_media_details获取详情，避免重复API调用
        media_details = get_tmdb_media_details(tmdb_id, 'tv_series', language)
        
        if not media_details:
            logger.error(f"❌ 无法获取TMDB电视剧详细信息: ID={tmdb_id}")
            return None
        
        # 如果已经处理过季度信息，直接返回
        if 'processed_seasons' in media_details:
            valid_seasons = media_details['processed_seasons']
            logger.info(f"✅ 使用已处理的TMDB季度信息，共{len(valid_seasons)}季")
            return valid_seasons
        
        # 如果没有处理过，手动处理季度信息
        seasons = media_details.get('seasons', [])
        valid_seasons = []
        for season in seasons:
            season_number = season.get('season_number', 0)
            if season_number > 0:  # 只保留正常季度
                valid_seasons.append({
                    'season_number': season_number,
                    'name': season.get('name', f'第{season_number}季'),
                    'episode_count': season.get('episode_count', 0),
                    'air_date': season.get('air_date', ''),
                    'overview': season.get('overview', '')
                })
        
        logger.info(f"✅ TMDB电视剧季度信息获取成功，共{len(valid_seasons)}季")
        return valid_seasons
        
    except Exception as e:
        logger.error(f"❌ TMDB季度信息获取失败: {e}")
        return None


def format_tmdb_media_info(tmdb_id: str, media_type: str) -> str:
    """格式化TMDB媒体详细信息用于显示
    
    Args:
        tmdb_id: TMDB媒体ID
        media_type: 媒体类型，'movie' 或 'tv_series'
        
    Returns:
        格式化的媒体信息字符串
    """
    media_details = get_tmdb_media_details(tmdb_id, media_type)
    
    if not media_details:
        return f"🎬 检测到 TMDB {'电视剧' if media_type == 'tv_series' else '电影'}\n\n❌ 无法获取详细信息"
    
    info_parts = []
    type_name = '电视剧' if media_type == 'tv_series' else '电影'
    info_parts.append(f"🎬 检测到 TMDB {type_name}")
    info_parts.append("")
    
    # 标题
    title = media_details.get('title') or media_details.get('name', '未知标题')
    info_parts.append(f"📋 标题: {title}")
    
    # 原标题（如果不同）
    original_title = media_details.get('original_title') or media_details.get('original_name')
    if original_title and original_title != title:
        info_parts.append(f"🌐 原标题: {original_title}")
    
    # 年份
    if media_type == 'movie':
        release_date = media_details.get('release_date', '')
        if release_date:
            year = release_date[:4]
            info_parts.append(f"📅 上映年份: {year}")
    else:
        first_air_date = media_details.get('first_air_date', '')
        if first_air_date:
            year = first_air_date[:4]
            info_parts.append(f"📅 首播年份: {year}")
        
        # 电视剧特有信息
        seasons = media_details.get('number_of_seasons')
        episodes = media_details.get('number_of_episodes')
        if seasons:
            info_parts.append(f"📺 季数: {seasons}季")
        if episodes:
            info_parts.append(f"🎞️ 总集数: {episodes}集")
    
    # 类型/流派
    genres = media_details.get('genres', [])
    if genres:
        genre_names = [g.get('name', '') for g in genres if g.get('name')]
        if genre_names:
            info_parts.append(f"🎭 类型: {', '.join(genre_names)}")
    
    # 评分
    vote_average = media_details.get('vote_average')
    if vote_average:
        info_parts.append(f"⭐ TMDB评分: {vote_average}/10")
    
    # 简介（截取前100字符）
    overview = media_details.get('overview', '')
    if overview:
        if len(overview) > 100:
            overview = overview[:100] + '...'
        info_parts.append(f"📝 简介: {overview}")
    
    return "\n".join(info_parts)