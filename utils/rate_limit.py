import logging
from typing import Dict, Optional, Any
from utils.api import call_danmaku_api

# 初始化日志
logger = logging.getLogger(__name__)


def get_rate_limit_status() -> Dict[str, Any]:
    """
    获取当前的限流状态信息
    
    Returns:
        Dict[str, Any]: 包含限流状态信息的字典
                        - success: 布尔值，表示获取状态是否成功
                        - data: 限流状态数据（如果成功）
                        - error: 错误信息（如果失败）
    """
    try:
        # 调用限流状态接口
        response = call_danmaku_api('GET', '/rate-limit/status')
        return response
    except Exception as e:
        error_msg = f"获取限流状态时发生异常：{str(e)[:50]}"
        logger.error(f"❌ {error_msg}")
        return {"success": False, "error": error_msg}


def should_block_by_rate_limit() -> tuple[bool, Optional[int]]:
    """
    检查是否应该根据限流状态阻止请求
    
    Returns:
        tuple[bool, Optional[int]]: (是否应该阻止请求, 重置倒计时秒数)
                                    - 第一个元素为True表示应该阻止请求（全局限流已禁用）
                                    - 第二个元素为secondsUntilReset值，如果不存在则为None
    """
    # 获取限流状态
    rate_limit_response = get_rate_limit_status()
    
    # 检查响应是否成功且包含数据
    if rate_limit_response.get('success') and rate_limit_response.get('data'):
        rate_limit_data = rate_limit_response['data']
        global_enabled = rate_limit_data.get('globalEnabled', True)
        seconds_until_reset = rate_limit_data.get('secondsUntilReset')
        
        # 如果全局限流已禁用，返回True表示应该阻止请求
        if not global_enabled:
            logger.info(f"🚫 全局限流已禁用，跳过操作流程")
            return True, seconds_until_reset
        
        # 记录限流状态信息
        logger.info(f"✅ 全局限流状态：已启用 (当前请求数: {rate_limit_data.get('globalRequestCount', 0)}/{rate_limit_data.get('globalLimit', 0)})")
        return False, None
    else:
        # 获取限流状态失败，默认允许继续处理
        error_msg = rate_limit_response.get('error', '未知错误')
        logger.warning(f"⚠️ 获取限流状态失败：{error_msg}，默认继续处理")
        return False, None
