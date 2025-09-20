import os
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from dotenv import load_dotenv
from utils.security import mask_sensitive_data

# 加载.env文件中的环境变量
load_dotenv()

# 配置日志 - 使用app/logs目录
log_dir = Path("app/logs")
log_dir.mkdir(parents=True, exist_ok=True)

# 导入RotatingFileHandler以支持日志文件大小限制
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(log_dir / "app.log", maxBytes=5*1024*1024, backupCount=1),  # 最大5MB，保留1个备份
        logging.StreamHandler()
    ]
)

# 设置第三方库的日志级别为 WARNING，减少敏感信息泄露
logging.getLogger('urllib3').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    """Telegram Bot 配置"""
    bot_token: str
    allowed_user_ids: List[int]
    admin_user_ids: List[int] = field(default_factory=list)
    connect_timeout: float = 30.0
    read_timeout: float = 30.0
    pool_timeout: float = 60.0
    connection_pool_size: int = 20
    
    def __post_init__(self):
        if not self.bot_token:
            raise ValueError("❌ TELEGRAM_BOT_TOKEN 不能为空")
        if not self.allowed_user_ids:
            raise ValueError("❌ ALLOWED_USER_IDS 不能为空")
        
        # 如果没有配置管理员，则所有允许的用户都是管理员
        if not self.admin_user_ids:
            self.admin_user_ids = self.allowed_user_ids.copy()
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ 未配置ADMIN_USER_IDS，所有ALLOWED_USER_IDS都将作为管理员")
        
        # 验证超时配置
        if self.connect_timeout <= 0:
            logger.warning("⚠️ connect_timeout 必须大于0，使用默认值30.0")
            self.connect_timeout = 30.0
        if self.read_timeout <= 0:
            logger.warning("⚠️ read_timeout 必须大于0，使用默认值30.0")
            self.read_timeout = 30.0
        if self.pool_timeout <= 0:
            logger.warning("⚠️ pool_timeout 必须大于0，使用默认值60.0")
            self.pool_timeout = 60.0
        if self.connection_pool_size <= 0:
            logger.warning("⚠️ connection_pool_size 必须大于0，使用默认值20")
            self.connection_pool_size = 20
        



@dataclass
class DanmakuAPIConfig:
    """弹幕API配置"""
    base_url: str
    api_key: str
    timeout: int = 60
    headers: Dict[str, str] = field(default_factory=lambda: {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    })
    
    def __post_init__(self):
        if not self.base_url:
            raise ValueError("❌ DANMAKU_API_BASE_URL 不能为空")
        if not self.api_key:
            raise ValueError("❌ DANMAKU_API_KEY 不能为空")
        
        # 验证超时配置
        if self.timeout <= 0:
            logger.warning("⚠️ API timeout 必须大于0，使用默认值60")
            self.timeout = 60
        
        # 确保URL格式正确
        if not self.base_url.startswith(('http://', 'https://')):
            raise ValueError("❌ DANMAKU_API_BASE_URL 必须以 http:// 或 https:// 开头")
        
        # 移除末尾的斜杠
        self.base_url = self.base_url.rstrip('/')


@dataclass
class TMDBConfig:
    """TMDB API 配置"""
    api_key: Optional[str] = None
    base_url: str = "https://api.themoviedb.org/3"
    proxy_url: Optional[str] = None  # TMDB代理URL，如果设置则使用代理而不是官方API
    
    def __post_init__(self):
        # 处理代理URL配置
        if self.proxy_url and self.proxy_url.strip():
            # 移除末尾的斜杠
            self.proxy_url = self.proxy_url.rstrip('/')
            # 如果配置了代理，使用代理URL作为base_url
            if self.proxy_url.endswith('/3'):
                self.base_url = self.proxy_url
            else:
                self.base_url = f"{self.proxy_url}/3"
            if not ConfigManager._initialization_logged:
                logger.info(f"ℹ️ 使用TMDB代理: {mask_sensitive_data(self.base_url)}")
        
        if not self.api_key or not self.api_key.strip():
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ 未配置 TMDB API Key，将跳过 TMDB 辅助搜索")
            return
            
        placeholder_values = ['your_tmdb_api_key_here', 'YOUR_TMDB_API_KEY', 'placeholder']
        if self.api_key.strip() in placeholder_values:
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ TMDB API Key为占位符值，请配置真实的API密钥")
            return
            
        # 验证API密钥
        if self._validate_api_key():
            if not ConfigManager._initialization_logged:
                logger.info("✅ TMDB API 配置已加载并验证通过，将启用辅助搜索功能")
        else:
            if not ConfigManager._initialization_logged:
                logger.info("❌ TMDB API Key验证失败，请检查密钥是否正确")
    
    @property
    def enabled(self) -> bool:
        """检查TMDB配置是否可用"""
        if not self.api_key or not self.api_key.strip():
            return False
        # 检查是否为占位符值
        placeholder_values = ['your_tmdb_api_key_here', 'YOUR_TMDB_API_KEY', 'placeholder']
        if self.api_key.strip() in placeholder_values:
            return False
        # 验证API密钥有效性
        return self._validate_api_key()
    
    def _validate_api_key(self) -> bool:
        """验证API密钥有效性"""
        try:
            import requests
            # 直接在这里验证，避免循环导入
            url = f"{self.base_url}/configuration"
            params = {'api_key': self.api_key}
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                return 'images' in data and 'base_url' in data.get('images', {})
            else:
                logger.debug(f"TMDB API密钥验证失败: HTTP {response.status_code}")
                return False
                
        except Exception as e:
            logger.debug(f"TMDB API密钥验证异常: {e}")
            return False


@dataclass
class TVDBConfig:
    """TVDB API 配置"""
    api_key: Optional[str] = None
    base_url: str = "https://api4.thetvdb.com/v4"
    
    def __post_init__(self):
        if not self.api_key or not self.api_key.strip():
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ 未配置 TVDB API Key，将跳过 TVDB 辅助搜索")
            return
            
        placeholder_values = ['your_tvdb_api_key_here', 'YOUR_TVDB_API_KEY', 'placeholder']
        if self.api_key.strip() in placeholder_values:
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ TVDB API Key为占位符值，请配置真实的API密钥")
            return
            
        if not ConfigManager._initialization_logged:
            logger.info("✅ TVDB API 配置已加载")
    
    @property
    def enabled(self) -> bool:
        """检查TVDB配置是否可用"""
        if not self.api_key or not self.api_key.strip():
            return False
        # 检查是否为占位符值
        placeholder_values = ['your_tvdb_api_key_here', 'YOUR_TVDB_API_KEY', 'placeholder']
        if self.api_key.strip() in placeholder_values:
            return False
        return True


@dataclass
class BGMConfig:
    """BGM (Bangumi) API 配置"""
    access_token: Optional[str] = None
    base_url: str = "https://api.bgm.tv"
    
    def __post_init__(self):
        if not self.access_token or not self.access_token.strip():
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ 未配置 BGM Access Token，将使用网页爬虫方式获取数据")
            return
            
        placeholder_values = ['your_bgm_access_token_here', 'YOUR_BGM_ACCESS_TOKEN', 'placeholder']
        if self.access_token.strip() in placeholder_values:
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ BGM Access Token为占位符值，将使用网页爬虫方式获取数据")
            return
            
        if not ConfigManager._initialization_logged:
            logger.info("✅ BGM API 配置已加载，将使用API方式获取数据")
    
    @property
    def enabled(self) -> bool:
        """检查BGM API配置是否可用"""
        if not self.access_token or not self.access_token.strip():
            return False
        # 检查是否为占位符值
        placeholder_values = ['your_bgm_access_token_here', 'YOUR_BGM_ACCESS_TOKEN', 'placeholder']
        if self.access_token.strip() in placeholder_values:
            return False
        return True


@dataclass
class ProxyConfig:
    """代理配置（使用Docker环境变量）"""
    
    def __post_init__(self):
        # 检查Docker代理环境变量
        http_proxy = os.getenv('HTTP_PROXY') or os.getenv('http_proxy')
        https_proxy = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy')
        
        if (http_proxy or https_proxy) and not ConfigManager._initialization_logged:
            logger.info(f"🌐 检测到Docker代理配置: HTTP_PROXY={http_proxy}, HTTPS_PROXY={https_proxy}")
    
    @property
    def enabled(self) -> bool:
        """检查代理是否启用（基于Docker环境变量）"""
        return bool(os.getenv('HTTP_PROXY') or os.getenv('http_proxy') or 
                   os.getenv('HTTPS_PROXY') or os.getenv('https_proxy'))
    
    @property
    def url(self) -> Optional[str]:
        """获取代理URL（优先返回HTTPS代理）"""
        return (os.getenv('HTTPS_PROXY') or os.getenv('https_proxy') or 
                os.getenv('HTTP_PROXY') or os.getenv('http_proxy'))


@dataclass
class WebhookConfig:
    """Webhook配置"""
    port: int = 7769
    api_key: str = ""
    enabled: bool = False
    play_event_cooldown_hours: int = 1  # 播放事件冷却时间（固定1小时），避免重复处理
    
    # 回调通知配置
    callback_enabled: bool = True  # 回调通知开关，默认启用
    callback_chat_id: str = ""  # 回调通知目标聊天ID
    
    def __post_init__(self):
        """初始化后验证配置"""
        if not self.api_key or not self.api_key.strip():
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ 未配置 WEBHOOK_API_KEY，webhook功能将被禁用")
            self.enabled = False
            return
            
        placeholder_values = ['your_webhook_api_key_here', 'YOUR_WEBHOOK_API_KEY', 'placeholder']
        if self.api_key.strip() in placeholder_values:
            if not ConfigManager._initialization_logged:
                logger.info("ℹ️ WEBHOOK_API_KEY 为占位符值，webhook功能将被禁用")
            self.enabled = False
            return
            
        # 验证端口范围
        if not (1024 <= self.port <= 65535):
            logger.warning(f"⚠️ webhook端口 {self.port} 不在有效范围内(1024-65535)，使用默认值7769")
            self.port = 7769
            
        self.enabled = True
        if not ConfigManager._initialization_logged:
            logger.info(f"✅ Webhook配置已启用，监听端口: {self.port}")
    
    def validate_callback_with_admin_ids(self, admin_user_ids: List[int] = None):
        """使用管理员ID验证回调配置
        
        Args:
            admin_user_ids: 管理员用户ID列表
        """
        self._validate_callback_config(admin_user_ids)
        if self.callback_enabled and not ConfigManager._initialization_logged:
            logger.info(f"✅ 回调通知已启用，目标聊天ID: {mask_sensitive_data(self.callback_chat_id)}")
    
    def _validate_callback_config(self, admin_user_ids: List[int] = None):
        """验证回调通知配置
        
        Args:
            admin_user_ids: 管理员用户ID列表，用于自动设置默认回调聊天ID
        """
        # 如果没有配置callback_chat_id或为占位符，尝试使用第一个管理员ID
        placeholder_chat_ids = ['your_chat_id_here', 'YOUR_CHAT_ID', 'placeholder', '']
        if not self.callback_chat_id or self.callback_chat_id.strip() in placeholder_chat_ids:
            if admin_user_ids and len(admin_user_ids) > 0:
                self.callback_chat_id = str(admin_user_ids[0])
                if not ConfigManager._initialization_logged:
                    logger.info(f"ℹ️ 未配置CALLBACK_CHAT_ID，自动使用第一个管理员ID: {admin_user_ids[0]}")
            else:
                if not ConfigManager._initialization_logged:
                    logger.info("ℹ️ 未配置回调通知参数且无管理员配置，回调通知功能将被禁用")
                self.callback_enabled = False
                return
            
        self.callback_enabled = True


@dataclass
class AppConfig:
    """应用程序配置"""
    log_level: str = "INFO"
    debug: bool = False
    environment: str = "production"
    api_timeout: int = 60
    
    def __post_init__(self):
        # 验证日志级别
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        self.log_level = self.log_level.upper()
        if self.log_level not in valid_levels:
            logger.warning(f"⚠️ 无效的日志级别 '{self.log_level}'，使用默认值 'INFO'")
            self.log_level = "INFO"
        
        # 设置调试模式
        if self.environment.lower() in ['dev', 'development', 'debug']:
            self.debug = True
            if self.log_level == "INFO":
                self.log_level = "DEBUG"


class ConfigManager:
    """配置管理器"""
    
    # 类级别标志，用于控制初始化日志记录
    _initialization_logged = False
    
    def __init__(self):
        # 使用app/config目录存储配置文件
        self.config_file_path = Path("app/config/config.json")
        self.user_config_file_path = Path("app/config/user.json")
        self._json_config = {}
        self._user_config = {}
        self._load_json_config()
        self._load_user_config()
        self._telegram: Optional[TelegramConfig] = None
        self._danmaku_api: Optional[DanmakuAPIConfig] = None
        self._tmdb: Optional[TMDBConfig] = None
        self._tvdb: Optional[TVDBConfig] = None
        self._bgm: Optional[BGMConfig] = None
        self._webhook: Optional[WebhookConfig] = None
        self._proxy: Optional[ProxyConfig] = None
        self._app: Optional[AppConfig] = None
        self._load_config()
    
    def _load_json_config(self):
        """加载JSON配置文件"""
        if not self.config_file_path.exists():
            if not ConfigManager._initialization_logged:
                logger.info(f"ℹ️ JSON配置文件不存在，将创建默认配置: {self.config_file_path}")
            self._create_default_config()
            return
        
        try:
            import json
            with open(self.config_file_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                self._json_config = config_data
                if not ConfigManager._initialization_logged:
                    logger.info(f"✅ JSON配置文件已加载: {self.config_file_path}")
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON配置文件格式错误: {e}")
            self._json_config = {}
        except Exception as e:
            logger.error(f"❌ 加载JSON配置文件失败: {e}")
            self._json_config = {}
    
    def _load_user_config(self):
        """加载用户配置文件"""
        if not self.user_config_file_path.exists():
            if not ConfigManager._initialization_logged:
                logger.info(f"ℹ️ 用户配置文件不存在，将创建默认配置: {self.user_config_file_path}")
            self._create_default_user_config()
            return
        
        try:
            import json
            with open(self.user_config_file_path, 'r', encoding='utf-8') as f:
                self._user_config = json.load(f)
                if not ConfigManager._initialization_logged:
                    logger.info(f"✅ 用户配置文件已加载: {self.user_config_file_path}")
        except json.JSONDecodeError as e:
            logger.error(f"❌ 用户配置文件格式错误: {e}")
            self._user_config = {"allowed_user_ids": [], "admin_user_ids": []}
        except Exception as e:
            logger.error(f"❌ 加载用户配置文件失败: {e}")
            self._user_config = {"allowed_user_ids": [], "admin_user_ids": []}
    
    def _create_default_config(self):
        """创建默认配置文件"""
        try:
            import json
            # 确保目录存在
            self.config_file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 创建默认配置
            default_config = {
                "telegram": {
                    "connect_timeout": 30.0,
                    "read_timeout": 30.0,
                    "pool_timeout": 60.0,
                    "connection_pool_size": 20
                },
                "danmaku_api": {
                    "timeout": 60,
                    "headers": {
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                    }
                },
                "tmdb": {
                    "base_url": "https://api.themoviedb.org/3"
                },
                "tvdb": {
                    "base_url": "https://api4.thetvdb.com/v4"
                },
                "bgm": {
                    "base_url": "https://api.bgm.tv"
                },
                "webhook": {
                    "port": 7769,
                    "enabled": False
                },
                "app": {
                    "log_level": "INFO",
                    "debug": False,
                    "environment": "production",
                    "api_timeout": 60
                }
            }
            
            with open(self.config_file_path, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=2, ensure_ascii=False)
            
            self._json_config = default_config
            logger.info(f"✅ 默认配置文件已创建: {self.config_file_path}")
            
        except Exception as e:
            logger.error(f"❌ 创建默认配置文件失败: {e}")
            self._json_config = {}
    
    def _create_default_user_config(self):
        """创建默认用户配置文件"""
        try:
            import json
            # 确保目录存在
            self.user_config_file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 创建默认用户配置
            default_user_config = {
                "allowed_user_ids": [],
                "admin_user_ids": []
            }
            
            with open(self.user_config_file_path, 'w', encoding='utf-8') as f:
                json.dump(default_user_config, f, indent=2, ensure_ascii=False)
            
            self._user_config = default_user_config
            logger.info(f"✅ 默认用户配置文件已创建: {self.user_config_file_path}")
            
        except Exception as e:
            logger.error(f"❌ 创建默认用户配置文件失败: {e}")
            self._user_config = {"allowed_user_ids": [], "admin_user_ids": []}
     
    def _save_user_config(self):
        """保存用户配置文件"""
        try:
            import json
            # 确保目录存在
            self.user_config_file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 准备保存的数据
            save_data = {
                "allowed_user_ids": self._telegram.allowed_user_ids if self._telegram else [],
                "admin_user_ids": self._telegram.admin_user_ids if self._telegram else []
            }
            
            with open(self.user_config_file_path, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"✅ 用户配置已保存: {self.user_config_file_path}")
            return True
        except Exception as e:
            logger.error(f"❌ 保存用户配置失败: {e}")
            return False
    
    def _parse_user_ids(self, user_ids_str: str) -> List[int]:
        """解析用户ID字符串"""
        if not user_ids_str:
            return []
        
        user_ids = []
        for id_str in user_ids_str.split(","):
            id_str = id_str.strip()
            if id_str.isdigit():
                user_ids.append(int(id_str))
            else:
                logger.warning(f"⚠️ 忽略无效的用户ID: {id_str}")
        
        return user_ids
    
    def _load_config(self):
        """加载配置"""
        try:
            # 加载Telegram配置（优先使用用户配置文件中的用户ID）
            telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            
            # 优先从用户配置文件读取用户ID，环境变量作为备选
            user_ids = self._user_config.get("allowed_user_ids", [])
            if not user_ids:  # 只有当JSON文件中没有配置时才使用环境变量
                user_ids_str = os.getenv("ALLOWED_USER_IDS", "")
                if user_ids_str.strip():
                    user_ids = self._parse_user_ids(user_ids_str)
            
            admin_ids = self._user_config.get("admin_user_ids", [])
            if not admin_ids:  # 只有当JSON文件中没有配置时才使用环境变量
                admin_ids_str = os.getenv("ADMIN_USER_IDS", "")
                if admin_ids_str.strip():
                    admin_ids = self._parse_user_ids(admin_ids_str)
            
            self._telegram = TelegramConfig(
                bot_token=telegram_token,
                allowed_user_ids=user_ids,
                admin_user_ids=admin_ids,
                connect_timeout=float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", 30.0)),
                read_timeout=float(os.getenv("TELEGRAM_READ_TIMEOUT", 30.0)),
                pool_timeout=float(os.getenv("TELEGRAM_POOL_TIMEOUT", 60.0)),
                connection_pool_size=int(os.getenv("TELEGRAM_CONNECTION_POOL_SIZE", 20))
            )
            
            # 加载弹幕API配置
            self._danmaku_api = DanmakuAPIConfig(
                base_url=os.getenv("DANMAKU_API_BASE_URL", ""),
                api_key=os.getenv("DANMAKU_API_KEY", ""),
                timeout=int(os.getenv("API_TIMEOUT", 60))
            )
            
            # 加载TMDB配置
            self._tmdb = TMDBConfig(
                api_key=os.getenv("TMDB_API_KEY", ""),
                proxy_url=os.getenv("TMDB_PROXY_URL", "")
            )
            
            # 加载TVDB配置
            self._tvdb = TVDBConfig(
                api_key=os.getenv("TVDB_API_KEY", "")
            )
            
            # 加载BGM配置
            self._bgm = BGMConfig(
                access_token=os.getenv("BGM_ACCESS_TOKEN", "")
            )
            
            # 加载Webhook配置
            self._webhook = WebhookConfig(
                port=int(os.getenv("WEBHOOK_PORT", 7769)),
                api_key=os.getenv("WEBHOOK_API_KEY", ""),
                callback_enabled=os.getenv("WEBHOOK_CALLBACK_ENABLED", "true").lower() in ['true', '1', 'yes'],
                callback_chat_id=os.getenv("WEBHOOK_CALLBACK_CHAT_ID", "")
            )
            
            # 验证回调通知配置（需要在telegram配置加载后）
            self._webhook.validate_callback_with_admin_ids(self._telegram.admin_user_ids)
            
            # 加载代理配置
            self._proxy = ProxyConfig()
            
            # 加载应用配置
            self._app = AppConfig(
                log_level=os.getenv("LOG_LEVEL", "INFO"),
                debug=os.getenv("DEBUG", "false").lower() in ['true', '1', 'yes'],
                environment=os.getenv("ENVIRONMENT", "production"),
                api_timeout=int(os.getenv("API_TIMEOUT", 60))
            )
            
            # 动态设置日志级别
            self._update_logging_level()
            
            if not ConfigManager._initialization_logged:
                logger.info("✅ 配置加载成功")
                ConfigManager._initialization_logged = True
            
        except Exception as e:
            logger.error(f"❌ 配置加载失败: {e}")
            raise
    
    def _update_logging_level(self):
        """根据配置动态更新日志级别"""
        try:
            # 获取当前日志级别
            log_level = getattr(logging, self._app.log_level, logging.INFO)
            
            # 更新根日志记录器的级别
            root_logger = logging.getLogger()
            root_logger.setLevel(log_level)
            
            # 更新所有处理器的级别
            for handler in root_logger.handlers:
                handler.setLevel(log_level)
            
            # 如果是DEBUG级别，记录调试信息
            if self._app.log_level == "DEBUG":
                logger.debug(f"🔧 日志级别已设置为: {self._app.log_level}")
                
        except Exception as e:
            logger.error(f"❌ 更新日志级别失败: {e}")
    
    @property
    def telegram(self) -> TelegramConfig:
        """获取Telegram配置"""
        if self._telegram is None:
            raise RuntimeError("Telegram配置未初始化")
        return self._telegram
    
    @property
    def danmaku_api(self) -> DanmakuAPIConfig:
        """获取弹幕API配置"""
        if self._danmaku_api is None:
            raise RuntimeError("弹幕API配置未初始化")
        return self._danmaku_api
    
    @property
    def tmdb(self) -> TMDBConfig:
        """获取TMDB配置"""
        if self._tmdb is None:
            raise RuntimeError("TMDB配置未初始化")
        return self._tmdb
    
    @property
    def tvdb(self) -> TVDBConfig:
        """获取TVDB配置"""
        if self._tvdb is None:
            raise RuntimeError("TVDB配置未初始化")
        return self._tvdb
    
    @property
    def bgm(self) -> BGMConfig:
        """获取BGM配置"""
        if self._bgm is None:
            raise RuntimeError("配置未初始化")
        return self._bgm
    
    @property
    def webhook(self) -> WebhookConfig:
        """获取Webhook配置"""
        if self._webhook is None:
            raise RuntimeError("配置未初始化")
        return self._webhook
    
    @property
    def proxy(self) -> ProxyConfig:
        """获取代理配置"""
        if self._proxy is None:
            raise RuntimeError("代理配置未初始化")
        return self._proxy
    
    @property
    def app(self) -> AppConfig:
        """获取应用配置"""
        if self._app is None:
            raise RuntimeError("应用配置未初始化")
        return self._app
    
    def reload(self):
        """重新加载配置"""
        load_dotenv(override=True)
        self._load_config()
        logger.info("🔄 配置已重新加载")
    
    def add_allowed_user(self, user_id: int) -> bool:
        """添加允许的用户ID
        
        Args:
            user_id: 用户ID
            
        Returns:
            bool: 是否添加成功
        """
        if not isinstance(user_id, int) or user_id <= 0:
            logger.warning(f"⚠️ 无效的用户ID: {user_id}")
            return False
            
        if user_id in self._telegram.allowed_user_ids:
            logger.info(f"ℹ️ 用户ID {user_id} 已在允许列表中")
            return True
            
        self._telegram.allowed_user_ids.append(user_id)
        logger.info(f"✅ 已添加用户ID {user_id} 到允许列表")
        # 自动保存用户配置到JSON文件
        self._save_user_config()
        return True
    
    def remove_allowed_user(self, user_id: int) -> bool:
        """移除允许的用户ID
        
        Args:
            user_id: 用户ID
            
        Returns:
            bool: 是否移除成功
        """
        if not isinstance(user_id, int) or user_id <= 0:
            logger.warning(f"⚠️ 无效的用户ID: {user_id}")
            return False
            
        # 检查是否为管理员，管理员不能被移除
        if user_id in self._telegram.admin_user_ids:
            logger.warning(f"⚠️ 不能移除管理员用户ID: {user_id}")
            return False
            
        if user_id not in self._telegram.allowed_user_ids:
            logger.info(f"ℹ️ 用户ID {user_id} 不在允许列表中")
            return True
            
        self._telegram.allowed_user_ids.remove(user_id)
        logger.info(f"✅ 已从允许列表移除用户ID {user_id}")
        # 自动保存用户配置到JSON文件
        self._save_user_config()
        return True
    
    def get_allowed_users(self) -> List[int]:
        """获取允许的用户ID列表"""
        return self._telegram.allowed_user_ids.copy()
    
    def get_admin_users(self) -> List[int]:
        """获取管理员用户ID列表"""
        return self._telegram.admin_user_ids.copy()
    
    def is_user_allowed(self, user_id: int) -> bool:
        """检查用户是否被允许"""
        return user_id in self._telegram.allowed_user_ids
    
    def is_user_admin(self, user_id: int) -> bool:
        """检查用户是否为管理员"""
        return user_id in self._telegram.admin_user_ids
    
    def get_config_summary(self) -> Dict[str, Any]:
        """获取配置摘要（隐藏敏感信息）"""
        return {
            "telegram": {
                "bot_token": "***" + self.telegram.bot_token[-4:] if self.telegram.bot_token else "未配置",
                "allowed_users_count": len(self.telegram.allowed_user_ids),
                "timeouts": {
                    "connect": self.telegram.connect_timeout,
                    "read": self.telegram.read_timeout,
                    "pool": self.telegram.pool_timeout
                },
                "pool_size": self.telegram.connection_pool_size
            },
            "danmaku_api": {
                "base_url": self.danmaku_api.base_url,
                "api_key": "***" + self.danmaku_api.api_key[-4:] if self.danmaku_api.api_key else "未配置",
                "timeout": self.danmaku_api.timeout
            },
            "tmdb": {
                "enabled": self.tmdb.enabled,
                "api_key": "***" + self.tmdb.api_key[-4:] if self.tmdb.api_key else "未配置",
                "base_url": self.tmdb.base_url
            },
            "tvdb": {
                "enabled": self.tvdb.enabled,
                "api_key": "***" + self.tvdb.api_key[-4:] if self.tvdb.api_key else "未配置",
                "base_url": self.tvdb.base_url
            },
            "bgm": {
                "enabled": self.bgm.enabled,
                "base_url": self.bgm.base_url,
                "has_access_token": bool(self.bgm.access_token)
            },
            "webhook": {
                "enabled": self.webhook.enabled,
                "port": self.webhook.port,
                "has_api_key": bool(self.webhook.api_key)
            },
            "proxy": {
                "enabled": self.proxy.enabled,
                "url": self.proxy.url if self.proxy.enabled else "未配置"
            },
            "app": {
                "log_level": self.app.log_level,
                "debug": self.app.debug,
                "environment": self.app.environment
            }
        }


# 创建全局配置实例
config = ConfigManager()

# 向后兼容的变量（保持现有代码正常工作）
TELEGRAM_BOT_TOKEN = config.telegram.bot_token
ALLOWED_USER_IDS = config.telegram.allowed_user_ids
ADMIN_USER_IDS = config.telegram.admin_user_ids
DANMAKU_API_BASE_URL = config.danmaku_api.base_url
DANMAKU_API_KEY = config.danmaku_api.api_key
DANMAKU_API_HEADERS = config.danmaku_api.headers
API_TIMEOUT = config.danmaku_api.timeout
# 代理配置现在通过Docker环境变量处理
# SOCKS_PROXY_URL 和 HTTP_PROXY_URL 已废弃，请使用 HTTP_PROXY 和 HTTPS_PROXY
TELEGRAM_CONNECT_TIMEOUT = config.telegram.connect_timeout
TELEGRAM_READ_TIMEOUT = config.telegram.read_timeout
TELEGRAM_POOL_TIMEOUT = config.telegram.pool_timeout
TELEGRAM_CONNECTION_POOL_SIZE = config.telegram.connection_pool_size
LOG_LEVEL = config.app.log_level

# TMDB配置
TMDB_API_KEY = config.tmdb.api_key
TMDB_BASE_URL = config.tmdb.base_url
TMDB_ENABLED = config.tmdb.enabled

# TVDB配置
TVDB_API_KEY = config.tvdb.api_key
TVDB_BASE_URL = config.tvdb.base_url
TVDB_ENABLED = config.tvdb.enabled

# BGM配置
BGM_ACCESS_TOKEN = config.bgm.access_token
BGM_BASE_URL = config.bgm.base_url
BGM_ENABLED = config.bgm.enabled

# Webhook配置
WEBHOOK_PORT = config.webhook.port
WEBHOOK_API_KEY = config.webhook.api_key
WEBHOOK_ENABLED = config.webhook.enabled