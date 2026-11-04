"""Conservative output-retrieval intent; never infer arbitrary local file paths."""

import re
from pathlib import Path

TYPES = {
    "word": {".docx"}, "docx": {".docx"}, "pdf": {".pdf"},
    "excel": {".xlsx", ".csv", ".tsv"}, "xlsx": {".xlsx"},
    "ppt": {".pptx"}, "powerpoint": {".pptx"}, "pptx": {".pptx"},
    "图片": {".png", ".jpg", ".jpeg", ".webp"}, "image": {".png", ".jpg", ".jpeg", ".webp"},
}


def is_retrieval(text):
    value = text.casefold().strip()
    if re.search(r"\b(create|generate|write|edit|modify|draft|make)\b|生成|创建|修改|撰写|制作|新建|写一", value):
        return False
    action = re.search(r"^(?:please\s+)?(?:send|retrieve|download|get)\b|^(?:请)?(?:把|将|发给我|发送|取回|下载)", value)
    target = re.search(r"\b(?:file|files|document|word|pdf|excel|ppt|powerpoint|image)\b|文件|文档|图片|[.](?:docx|pdf|xlsx|pptx|png|txt|md)\b", value)
    return bool(action and target)


def matching_outputs(text, rows):
    value = text.casefold()
    named = [r for r in rows if Path(r["path"]).name.casefold() in value]
    explicit = re.findall(r"[^\s<>\"'|,，]+\.(?:docx|pdf|xlsx|pptx|png|jpg|jpeg|webp|txt|md|csv|tsv)(?=$|[^a-z0-9])", value)
    if explicit and any(not any(token.endswith(Path(r['path']).name.casefold())
                               or Path(r['path']).name.casefold().endswith(token) for r in named) for token in explicit):
        return []
    if named:
        return named
    suffixes = set()
    for word, choices in TYPES.items():
        if (word in value if not word.isascii() else re.search(r"\b" + word + r"\b", value)):
            suffixes.update(choices)
    return [r for r in rows if not suffixes or Path(r["path"]).suffix.casefold() in suffixes]
