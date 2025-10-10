import os
import logging
import re
from typing import Dict, Optional, Tuple
from utils.api import call_danmaku_api

logger = logging.getLogger(__name__)

def convert_emby_series_name(series_name: str, parent_index_number: int) -> Optional[Dict[str, any]]:
    """
    根据Emby通知中的SeriesName和ParentIndexNumber进行名称转换
    
    Args:
        series_name: Emby中的剧集名称，如"奔跑吧兄弟"
        parent_index_number: Emby中的季度编号，如13
        
    Returns:
        转换结果字典，包含series_name和season_number，如果没有匹配则返回None
        {
            "series_name": "奔跑吧",
            "season_number": 9
        }
    """
    if not series_name or parent_index_number is None:
        return None
        
    # 生成待匹配词：支持S13和S13两种格式（反向匹配）
    match_patterns = [
        f"{series_name} S{parent_index_number}",  # 如：奔跑吧兄弟 S13
        f"{series_name} S{parent_index_number:02d}",  # 如：奔跑吧兄弟 S13
        series_name  # 简单替换模式，只匹配剧集名称
    ]
    
    logger.debug(f"🔍 生成匹配模式: {match_patterns}")
    
    try:
        # 通过API获取识别词配置
        config_content = _get_title_recognition_config()
        if not config_content:
            logger.warning("⚠️ 无法获取识别词配置")
            return None
            
        # 按行分割配置内容
        lines = config_content.split('\n')
            
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            
            # 跳过空行和注释行
            if not line or line.startswith('#'):
                continue
                
            # 解析映射规则
            if ' => ' not in line:
                logger.warning(f"⚠️ 配置第{line_num}行格式错误，跳过: {line}")
                continue
                
            try:
                # 分割左右两部分
                left_part, right_part = line.split(' => ', 1)
                left_part = left_part.strip()
                right_part = right_part.strip()
                
                # 检查是否为复杂模式（包含大括号）
                if right_part.startswith('{') and right_part.endswith('}'):
                    # 复杂模式：解析季度偏移规则
                    result = _parse_complex_rule(left_part, right_part, series_name, parent_index_number)
                    if result:
                        logger.info(f"🔄 复杂模式转换成功: {series_name} S{parent_index_number} -> {result}")
                        return result
                else:
                    # 简单模式：直接匹配和替换
                    result = _parse_simple_rule(left_part, right_part, match_patterns)
                    if result:
                        logger.info(f"🔄 简单模式转换成功: {series_name} -> {result}")
                        return result
                            
            except Exception as e:
                logger.warning(f"⚠️ 配置第{line_num}行解析错误，跳过: {line} ({e})")
                
        logger.debug(f"🔍 未找到匹配规则: {match_patterns}")
        return None
        
    except Exception as e:
        logger.error(f"❌ 获取识别词配置失败: {e}")
        return None

def _get_title_recognition_config() -> Optional[str]:
    """
    通过API获取titleRecognition配置内容
    
    Returns:
        配置内容字符串或None
    """
    try:
        # 调用/config接口获取配置
        response = call_danmaku_api(
            method="GET",
            endpoint="/config"
        )
        
        if not response.get("success"):
            logger.error(f"❌ API调用失败: {response.get('error', '未知错误')}")
            return None
            
        data = response.get("data", {})
        configs = data.get("configs", [])
        
        # 查找titleRecognition配置项
        for config in configs:
            if config.get("key") == "titleRecognition":
                title_recognition_value = config.get("value", "")
                return title_recognition_value
                
        logger.warning("⚠️ 未找到titleRecognition配置项")
        return None
        
    except Exception as e:
        logger.error(f"❌ 获取识别词配置异常: {e}")
        return None

def _parse_complex_rule(left_part: str, right_part: str, series_name: str, parent_index_number: int) -> Optional[Dict[str, any]]:
    """
    解析复杂规则（季度偏移模式）
    
    Args:
        left_part: 规则左侧，如"奔跑吧 S09"
        right_part: 规则右侧，如"{[source=tencent;title=奔跑吧兄弟;season_offset=9>13]}"
        series_name: 当前剧集名称
        parent_index_number: 当前季度编号
        
    Returns:
        转换结果字典或None
    """
    try:
        # 解析右侧复杂规则
        content = right_part[1:-1]  # 去掉大括号
        if not content.startswith('[') or not content.endswith(']'):
            return None
            
        content = content[1:-1]  # 去掉方括号
        params = {}
        
        # 解析参数
        for param in content.split(';'):
            if '=' in param:
                key, value = param.split('=', 1)
                params[key.strip()] = value.strip()
        
        target_title = params.get('title', '')
        season_offset = params.get('season_offset', '')
        
        if not target_title or not season_offset:
            return None
            
        # 检查是否匹配目标剧集名称和季度
        # 去除季度信息后进行完全匹配
        # 支持多种季度格式：第X季、Season X、S0X、SX、季X、第X部
        season_patterns = [
            r'\s*第\d+季\s*',           # 第1季、第01季
            r'\s*Season\s*\d+\s*',      # Season 1、Season1
            r'\s*S\d+\s*',              # S1、S01
            r'\s*季\d+\s*',             # 季1
            r'\s*第\d+部\s*',           # 第1部、第01部
            r'\s*Part\s*\d+\s*',        # Part 1、Part1
            r'\s*\d+季\s*',             # 1季
            r'\s*\d+部\s*',             # 1部
            r'\s*Vol\.\s*\d+\s*',       # Vol.1、Vol. 1
            r'\s*Volume\s*\d+\s*',      # Volume 1、Volume1
        ]
        
        clean_series_name = series_name
        clean_target_title = target_title
        
        # 逐个应用季度模式进行清理
        for pattern in season_patterns:
            clean_series_name = re.sub(pattern, '', clean_series_name, flags=re.IGNORECASE)
            clean_target_title = re.sub(pattern, '', clean_target_title, flags=re.IGNORECASE)
        
        # 清理多余的空格和标点符号
        clean_series_name = re.sub(r'\s+', ' ', clean_series_name).strip()
        clean_target_title = re.sub(r'\s+', ' ', clean_target_title).strip()
        
        if clean_series_name == clean_target_title:
            # 解析季度偏移运算
            source_season = _parse_season_offset(season_offset, parent_index_number)
            if source_season is not None:
                # 提取左侧的剧集名称
                left_series_name = _extract_series_name_from_left(left_part)
                if left_series_name:
                    return {
                        "series_name": left_series_name,
                        "season_number": source_season
                    }
        
        return None
        
    except Exception as e:
        logger.warning(f"⚠️ 复杂规则解析失败: {e}")
        return None

def _parse_simple_rule(left_part: str, right_part: str, match_patterns: list) -> Optional[Dict[str, any]]:
    """
    解析简单规则（直接替换模式）
    
    Args:
        left_part: 规则左侧
        right_part: 规则右侧
        match_patterns: 匹配模式列表
        
    Returns:
        转换结果字典或None
    """
    # 检查是否匹配右侧（反向匹配）
    for match_pattern in match_patterns:
        if match_pattern == right_part:
            logger.info(f"✅ 找到简单匹配规则: {match_pattern} <= {left_part}")
            
            # 解析左侧结果
            result = _parse_converted_result(left_part)
            if result:
                return result
            else:
                # 如果左侧不包含季度信息，则为纯名称替换
                # 从匹配模式中提取季度信息
                season_number = 1  # 默认季度
                
                # 尝试从匹配的模式中提取季度信息
                if ' S' in match_pattern:
                    try:
                        parts = match_pattern.split(' S')
                        if len(parts) == 2:
                            season_number = int(parts[1])
                    except (ValueError, IndexError):
                        season_number = 1
                
                return {
                    "series_name": left_part,
                    "season_number": season_number
                }
    
    return None

def _extract_series_name_from_left(left_part: str) -> Optional[str]:
    """
    从左侧规则中提取剧集名称
    
    Args:
        left_part: 左侧规则，如"奔跑吧 S09"
        
    Returns:
        剧集名称或None
    """
    # 匹配格式：剧集名称 S数字
    pattern = r'^(.+?)\s+S\d+$'
    match = re.match(pattern, left_part.strip())
    
    if match:
        return match.group(1).strip()
    else:
        # 如果没有季度信息，直接返回整个字符串
        return left_part.strip()

def _parse_season_offset(season_offset: str, current_season: int) -> Optional[int]:
    """
    解析季度偏移运算
    
    Args:
        season_offset: 偏移规则，如"9>13"、"9+4"、"9-4"
        current_season: 当前季度编号
        
    Returns:
        计算后的源季度编号或None
    """
    try:
        # 支持三种运算符：>、+、-
        if '>' in season_offset:
            # 直接映射：9>13 表示源季度9对应目标季度13
            source_str, target_str = season_offset.split('>', 1)
            source_season = int(source_str.strip())
            target_season = int(target_str.strip())
            
            if current_season == target_season:
                return source_season
                
        elif '+' in season_offset:
            # 加法运算：9+4 表示源季度9加4等于目标季度13
            source_str, offset_str = season_offset.split('+', 1)
            source_season = int(source_str.strip())
            offset = int(offset_str.strip())
            
            if current_season == source_season + offset:
                return source_season
                
        elif '-' in season_offset:
            # 减法运算：13-4 表示源季度13减4等于目标季度9
            source_str, offset_str = season_offset.split('-', 1)
            source_season = int(source_str.strip())
            offset = int(offset_str.strip())
            
            if current_season == source_season - offset:
                return source_season
        
        return None
        
    except (ValueError, IndexError) as e:
        logger.warning(f"⚠️ 季度偏移解析失败: {season_offset} ({e})")
        return None

def _parse_converted_result(converted_result: str) -> Optional[Dict[str, any]]:
    """
    解析转换结果，提取series_name和season_number
    
    Args:
        converted_result: 转换后的结果，如"奔跑吧 S09"
        
    Returns:
        解析结果字典或None
    """
    # 匹配格式：标题 S数字
    pattern = r'^(.+?)\s+S(\d+)$'
    match = re.match(pattern, converted_result.strip())
    
    if match:
        series_name = match.group(1).strip()
        season_number = int(match.group(2))
        
        return {
            "series_name": series_name,
            "season_number": season_number
        }
    else:
        logger.warning(f"⚠️ 无法解析转换结果格式: {converted_result}")
        return None