"""Deterministic, deliberately conservative classification; no LLM or inferred specs."""
import re
import json


def classify_product(title):
    text = str(title).lower()
    category = 'mac_mini' if re.search(r'mac\s*mini', text) else 'unknown'
    reason = None
    if re.search(r'空盒|仅.{0,3}(?:包装|盒子|电源线)|求购|收购|维修服务|扩容服务|租赁|出租', text):
        reason = '配件/包装/服务/求购，非完整商品'
    chip = re.findall(r'(?<![a-z0-9])m\d{1,2}(?:\s*(?:pro|max|ultra))?(?![a-z0-9])', text)
    ram = re.findall(r'(\d{1,3})\s*(?:gb|g)\s*(?:内存|ram|运行)|(?:内存|ram)\s*(\d{1,3})\s*(?:gb|g)?', text)
    ssd = re.findall(r'(\d{1,4})\s*(gb|g|tb|t)\s*(?:硬盘|ssd|固态)|(?:硬盘|ssd|固态)\s*(\d{1,4})\s*(gb|g|tb|t)', text)
    memories = {a or b for a, b in ram}
    disks = {str(int(a or c) * (1024 if (b or d).startswith('t') else 1)) for a, b, c, d in ssd if (b or d).startswith('t') or int(a or c) >= 128}
    # Common explicit pairs: 16GB/256GB, 24G+512G, 16+256. No lone number guesses.
    for match in re.finditer(r'(?<!\d)(\d{1,3})\s*(?:gb|g)?\s*[/+]\s*(\d{1,4})\s*(gb|g|tb|t)?(?!\d)', text):
        memories.add(match[1])
        disks.add(str(int(match[2]) * (1024 if (match[3] or '').startswith('t') else 1)))
    # Explicit standard ordered sizes, e.g. M4 16GB 256GB, if exactly one of each.
    sizes = re.findall(r'(?<!\d)(\d{1,4})\s*(gb|g|tb|t)\b', text)
    memories.update(n for n, unit in sizes if unit in {'g', 'gb'} and int(n) in {8, 16, 24, 32, 48, 64, 96, 128})
    disks.update(str(int(n) * (1024 if unit.startswith('t') else 1)) for n, unit in sizes if unit.startswith('t') or int(n) >= 256)
    chips = {re.sub(r'(M\d+)(PRO|MAX|ULTRA)', r'\1 \2', ' '.join(c.upper().split())) for c in chip}
    variant = 'unknown'
    if category == 'mac_mini' and len(chips) == len(memories) == len(disks) == 1:
        variant = f'Mac mini / {next(iter(chips))} / {next(iter(memories))}GB / {next(iter(disks))}GB'
    fault = bool(re.search(r'故障|不开机|无法开机|损坏|进水|维修机', re.sub(r'无故障|没有故障|无进水|未进水', '', text)))
    if fault:
        reason = '明显故障，默认排除统计'
    attributes = {
        'chip': next(iter(chips)) if len(chips) == 1 else None,
        'memory_gb': int(next(iter(memories))) if len(memories) == 1 else None,
        'ssd_gb': int(next(iter(disks))) if len(disks) == 1 else None,
        'condition': '全新' if '全新' in text and not re.search(r'非全新|不是全新', text) else ('二手' if '二手' in text else 'unknown'),
        'accessories': '缺主要配件' if re.search(r'不带电源|无电源|缺电源|不含电源', text) else '含主要配件' if re.search(r'原装电源|全套|带电源', text) else 'unknown',
        'warranty': '无保修描述' if re.search(r'不在保|无保修|已过保', text) else '有保修描述' if re.search(r'在保|保修至|保修到', text) else 'unknown',
        'obvious_fault': fault,
    }
    return {'product_category': category, 'product_variant': variant,
            'attributes_json': json.dumps(attributes, ensure_ascii=False),
            'excluded': int(bool(reason)), 'exclusion_reason': reason}
