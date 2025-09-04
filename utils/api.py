import logging
import requests
from typing import Dict, Optional, Any
from config import DANMAKU_API_BASE_URL, DANMAKU_API_KEY, API_TIMEOUT, DANMAKU_API_HEADERS

# 初始化日志
logger = logging.getLogger(__name__)

def call_danmaku_api(
    method: str,
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    调用Misaka Danmaku API的通用函数（修复URL拼接错误）
    """
    # 1. 拼接基础地址与端点（处理首尾斜杠）
    base_url_with_endpoint = f"{DANMAKU_API_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    
    # 2. 手动添加api_key参数（避免与其他参数冲突）
    if "?" in base_url_with_endpoint:
        full_url = f"{base_url_with_endpoint}&api_key={DANMAKU_API_KEY}"
    else:
        full_url = f"{base_url_with_endpoint}?api_key={DANMAKU_API_KEY}"

    params = params or {}
    try:
        response = requests.request(
            method=method.upper(),
            url=full_url,
            params=params,
            json=json_data,
            headers=DANMAKU_API_HEADERS,
            timeout=API_TIMEOUT,
            verify=True
        )
        response.raise_for_status()
        return {"success": True, "data": response.json()}

    except requests.exceptions.Timeout:
        logger.error(f"⏱️ API请求超时：{full_url}")
        return {"success": False, "error": "请求超时，请稍后重试"}
    except requests.exceptions.ConnectionError:
        logger.error(f"🔌 API连接失败：{full_url}")
        return {"success": False, "error": "API连接失败，请检查地址是否正确"}
    except requests.exceptions.HTTPError as e:
        error_msg = f"HTTP错误 {e.response.status_code}：{e.response.text[:100]}"
        logger.error(f"❌ API请求错误：{full_url}，{error_msg}")
        return {"success": False, "error": error_msg}
    except Exception as e:
        error_msg = f"未知错误：{str(e)[:50]}"
        logger.error(f"❌ API请求异常：{full_url}，{error_msg}")
        return {"success": False, "error": error_msg}