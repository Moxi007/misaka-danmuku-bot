# -*- coding: utf-8 -*-

import os
import logging
from pathlib import Path
from typing import List, Set

logger = logging.getLogger(__name__)

def initialize_blacklist_config() -> bool:
    """
    初始化黑名单配置文件
    
    在项目启动时创建 /app/config/blacklist.txt 文件（如果不存在）
    参考 identify.txt 的管理方式，提供持久化的黑名单存储
    
    Returns:
        bool: 初始化成功返回True，失败返回False
    """
    try:
        config_file = Path("app/config/blacklist.txt")
        
        # 确保目录存在
        config_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 如果文件不存在，创建文件并添加说明注释和示例数据
        if not config_file.exists():
            with open(config_file, 'w', encoding='utf-8') as f:
                f.write("# 黑名单影视名称文件\n")
                f.write("# 每行一个影视名称，包含该名称的影视将被阻止导入\n")
                f.write("# 示例：\n")
                f.write("# 测试黑名单电影\n")
                f.write("# 测试黑名单剧集\n\n")
                f.write("# 注意事项：\n")
                f.write("# - 每行一个影视名称\n")
                f.write("# - 以 # 开头的行为注释\n")
                f.write("# - 空行会被忽略\n")
                f.write("# - 匹配时不区分大小写\n")
            
            logger.info(f"✅ 黑名单配置文件已创建: {config_file.absolute()}")
        else:
            logger.info(f"✅ 黑名单配置文件已存在: {config_file.absolute()}")
        
        # 验证文件可读写
        if config_file.is_file() and os.access(config_file, os.R_OK | os.W_OK):
            logger.info(f"✅ 黑名单配置文件权限正常")
            return True
        else:
            logger.error(f"❌ 黑名单配置文件权限异常: {config_file.absolute()}")
            return False
            
    except Exception as e:
        logger.error(f"❌ 初始化黑名单配置文件失败: {e}")
        return False

def get_blacklist_config_path() -> Path:
    """
    获取黑名单配置文件路径
    
    Returns:
        Path: 配置文件路径对象
    """
    return Path("app/config/blacklist.txt")

def validate_blacklist_config() -> bool:
    """
    验证黑名单配置文件的有效性
    
    Returns:
        bool: 文件有效返回True，无效返回False
    """
    try:
        config_file = get_blacklist_config_path()
        
        if not config_file.exists():
            logger.warning(f"⚠️ 黑名单配置文件不存在: {config_file.absolute()}")
            return False
        
        if not config_file.is_file():
            logger.error(f"❌ 黑名单配置路径不是文件: {config_file.absolute()}")
            return False
        
        if not os.access(config_file, os.R_OK | os.W_OK):
            logger.error(f"❌ 黑名单配置文件权限不足: {config_file.absolute()}")
            return False
        
        # 尝试读取文件内容验证格式
        with open(config_file, 'r', encoding='utf-8') as f:
            content = f.read()
            logger.debug(f"📄 黑名单配置文件内容长度: {len(content)} 字符")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ 验证黑名单配置文件失败: {e}")
        return False

def load_blacklist() -> Set[str]:
    """
    加载黑名单影视名称列表
    
    Returns:
        Set[str]: 黑名单影视名称集合（小写形式）
    """
    blacklist = set()
    
    try:
        config_file = get_blacklist_config_path()
        
        # 确保配置文件存在
        if not config_file.exists():
            logger.warning(f"⚠️ 黑名单配置文件不存在，返回空列表: {config_file.absolute()}")
            return blacklist
        
        with open(config_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                
                # 跳过空行和注释行
                if not line or line.startswith('#'):
                    continue
                
                # 将黑名单名称转为小写存储，便于后续不区分大小写匹配
                blacklist.add(line.lower())
                logger.debug(f"📝 加载黑名单 {line_num}: {line}")
        
        logger.info(f"✅ 成功加载 {len(blacklist)} 个黑名单影视名称")
        
    except Exception as e:
        logger.error(f"❌ 加载黑名单失败: {e}")
    
    return blacklist

def add_blacklist_item(name: str) -> bool:
    """
    添加新的黑名单影视名称
    
    Args:
        name: 影视名称字符串
        
    Returns:
        bool: 添加成功返回True，失败返回False
    """
    try:
        config_file = get_blacklist_config_path()
        
        # 确保配置文件存在
        if not initialize_blacklist_config():
            return False
        
        # 检查名称是否为空
        if not name or not name.strip():
            logger.warning(f"⚠️ 黑名单名称不能为空")
            return False
        
        name = name.strip()
        
        # 检查规则是否已存在
        existing_blacklist = load_blacklist()
        if name.lower() in existing_blacklist:
            logger.warning(f"⚠️ 该影视名称已在黑名单中: {name}")
            return True  # 已存在也算成功
        
        # 追加新名称到文件
        with open(config_file, 'a', encoding='utf-8') as f:
            # 确保在添加内容前先检查最后一行是否已以换行符结束
            try:
                # 获取文件当前大小
                file_size = config_file.stat().st_size
                if file_size > 0:
                    # 读取最后一个字符检查是否是换行符
                    with open(config_file, 'rb') as fr:
                        fr.seek(file_size - 1)
                        last_char = fr.read(1)
                    # 如果最后一个字符不是换行符，先添加一个换行符
                    if last_char != b'\n':
                        f.write('\n')
            except Exception as e:
                logger.warning(f"检查文件换行符时出错: {e}")
            
            # 写入新的黑名单名称
            f.write(name)
            # 确保添加换行符到新名称后面
            f.write('\n')
        
        logger.info(f"✅ 黑名单影视名称已添加: {name}")
        return True
        
    except Exception as e:
        logger.error(f"❌ 添加黑名单影视名称失败: {e}")
        return False

def get_blacklist_stats() -> dict:
    """
    获取黑名单配置统计信息
    
    Returns:
        dict: 统计信息，包含文件状态、黑名单数量等
    """
    try:
        config_file = get_blacklist_config_path()
        file_exists = config_file.exists()
        file_size = config_file.stat().st_size if file_exists else 0
        blacklist_count = len(load_blacklist())
        
        return {
            'file_exists': file_exists,
            'file_path': str(config_file.absolute()),
            'file_size_bytes': file_size,
            'blacklist_count': blacklist_count,
            'file_readable': os.access(config_file, os.R_OK) if file_exists else False,
            'file_writable': os.access(config_file, os.W_OK) if file_exists else False
        }
        
    except Exception as e:
        logger.error(f"❌ 获取黑名单统计信息失败: {e}")
        return {
            'file_exists': False,
            'file_path': str(get_blacklist_config_path().absolute()),
            'file_size_bytes': 0,
            'blacklist_count': 0,
            'file_readable': False,
            'file_writable': False,
            'error': str(e)
        }