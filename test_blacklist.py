#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
测试黑名单添加功能
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.blacklist_config import add_blacklist_item, load_blacklist, get_blacklist_config_path


def test_blacklist():    
    # 打印当前黑名单内容
    print("=== 当前黑名单内容 ===")
    current_blacklist = load_blacklist()
    print(f"黑名单项目数: {len(current_blacklist)}")
    for item in current_blacklist:
        print(f"- {item}")
    
    # 测试添加新的黑名单项目
    test_name = "测试黑名单电影"
    print(f"\n=== 测试添加黑名单项目: {test_name} ===")
    success = add_blacklist_item(test_name)
    
    if success:
        print("✅ 添加成功！")
        
        # 再次加载并显示黑名单
        print("\n=== 添加后的黑名单内容 ===")
        updated_blacklist = load_blacklist()
        print(f"黑名单项目数: {len(updated_blacklist)}")
        for item in updated_blacklist:
            print(f"- {item}")
            
        # 验证文件内容
        config_path = get_blacklist_config_path()
        print(f"\n=== 文件内容验证 ===")
        print(f"文件路径: {config_path}")
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
            print(f"文件内容长度: {len(content)} 字符")
            print(f"包含测试名称: {test_name in content}")
            
            # 打印文件内容的最后几行
            lines = content.splitlines()
            print("文件最后5行:")
            for line in lines[-5:]:
                print(f"  {line}")
    else:
        print("❌ 添加失败！")


if __name__ == "__main__":
    test_blacklist()