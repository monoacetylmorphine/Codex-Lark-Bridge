# -*- coding: utf-8 -*-
import json
import re
import os
from collections import defaultdict

def extract_display_names_from_jsonp(content):
    """
    从 JSONP 格式的内容中提取 displayName 列表
    """
    # 去掉 JSONP 回调包装 (mtopjsonp46(...))
    match = re.search(r'\(({.*})\)', content, re.DOTALL)
    if not match:
        # 尝试直接解析纯 JSON（以防万一）
        try:
            data = json.loads(content)
        except:
            raise ValueError("无法解析 JSONP 数据")
    else:
        json_str = match.group(1)
        data = json.loads(json_str)
    
    result = data.get('data', {}).get('result', [])
    names = [item['displayName'] for item in result if 'displayName' in item]
    return names

def batch_process(directory):
    """
    处理指定目录下所有 '*-custom-list-*.json' 文件，按前缀汇总 displayName
    """
    user_names = defaultdict(list)
    
    # 获取所有符合条件的文件
    pattern = re.compile(r'^(.*?)-customer-list-\d+\.json$')
    
    for filename in os.listdir(directory):
        match = pattern.match(filename)
        if not match:
            continue
        
        prefix = match.group(1)  # 提取前缀，如 '悦吟', 'aaron'
        filepath = os.path.join(directory, filename)
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            names = extract_display_names_from_jsonp(content)
            user_names[prefix].extend(names)
        except Exception as e:
            print(f"处理文件 {filename} 时出错: {e}")
    
    # 将 defaultdict 转为普通 dict（可选）
    return dict(user_names)

if __name__ == '__main__':
    # 指定目录路径
    target_dir = '/Users/renxiaoyao/Documents/customer-2026-04-21-2026-07-29/customer-list'
    
    # 批量处理
    result = batch_process(target_dir)
    
    # 输出 JSON 文件
    output_file = os.path.join(target_dir, 'combined_display_names.json')
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"处理完成，结果已保存至: {output_file}")
    print(f"共处理 {len(result)} 个用户:")
    for user, names in result.items():
        print(f"  {user}: {len(names)} 个名称")