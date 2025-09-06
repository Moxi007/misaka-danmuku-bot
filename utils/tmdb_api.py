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