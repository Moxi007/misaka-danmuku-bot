# 从各个模块导入所需的处理器创建函数和命令处理函数
from .general import start_command, help_command, cancel_command
from .import_media import create_search_handler, create_import_auto_handler, create_episode_input_handler
from .identify_management import create_identify_handler
from .token_management import create_token_management_handler
from .refresh_sources import create_refresh_handler
from .import_url import create_import_url_handler
from .user_management import create_user_management_handler
from .blacklist_management import create_blacklist_handler
from .tasks import create_tasks_handler
from telegram.ext import CommandHandler

# 实现11个功能方法

def get_search_handler():
    """提供媒体搜索功能 (/search [关键词])"""
    return create_search_handler()

def get_auto_handler():
    """支持自动导入媒体 (/auto)"""
    return create_import_auto_handler()

def get_url_handler():
    """为现有数据源导入指定剧集 (/url)"""
    return create_import_url_handler()

def get_refresh_handler():
    """实现数据源刷新功能 (/refresh)"""
    return create_refresh_handler()

def get_tokens_handler():
    """提供API访问令牌管理功能 (/tokens)"""
    return create_token_management_handler()

def get_tasks_handler():
    """显示当前任务列表 (/tasks)"""
    return create_tasks_handler()

def get_users_handler():
    """实现用户权限管理功能 (/users)"""
    return create_user_management_handler()

def get_identify_handler():
    """提供识别词映射管理 (/identify)"""
    return create_identify_handler()

def get_blacklist_handler():
    """实现黑名单管理功能 (/blacklist)"""
    return create_blacklist_handler()

def get_help_handler():
    """显示帮助信息 (/help)"""
    from utils.handlers_utils import wrap_conversation_entry_point
    return CommandHandler("help", wrap_conversation_entry_point(help_command))

def get_cancel_handler():
    """提供取消当前操作的功能 (/cancel)"""
    from utils.handlers_utils import wrap_conversation_entry_point
    return CommandHandler("cancel", wrap_conversation_entry_point(cancel_command))

# 导出所有需要注册的处理器和功能方法
__all__ = [
    'start',
    'create_search_handler',
    'create_import_auto_handler',
    'create_episode_input_handler',
    'create_identify_handler',
    'create_token_management_handler',
    'create_refresh_handler',
    'create_import_url_handler',
    'create_user_management_handler',
    'create_blacklist_handler',
    'create_tasks_handler',
    'get_search_handler',
    'get_auto_handler',
    'get_url_handler',
    'get_refresh_handler',
    'get_tokens_handler',
    'get_tasks_handler',
    'get_users_handler',
    'get_identify_handler',
    'get_blacklist_handler',
    'get_help_handler',
    'get_cancel_handler'
]