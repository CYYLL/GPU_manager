"""Keep the requested GPU count separate from model supplied tool arguments."""

import re

REJECTION_PREFIX = "创建配置校验失败："
ZERO_GPU_REPLY = REJECTION_PREFIX + "系统不支持创建 0 张 GPU 的容器；未创建容器，也未分配 GPU。"


def zero_gpu_reply(min_gpu=None):
    if min_gpu is None:
        return ZERO_GPU_REPLY
    return (REJECTION_PREFIX + "该镜像预设的最低 GPU 数为 %s；系统不支持创建 0 张 GPU 的容器；"
            "未创建容器，也未分配 GPU。" % min_gpu)

_NUMBER = r"(\d+|[零一二两三四五六七八九十]+)"
_GPU_COUNT_PATTERNS = (
    re.compile(r"(?:gpu(?:_count)?|显卡)\s*(?:数量|个数|张数|设定|设置|设为|配置|分配|数|count)\s*"
               r"(?:为|成|到|=|:|：)?\s*" + _NUMBER, re.IGNORECASE),
    re.compile(r"(?:gpu(?:_count)?|显卡)\s*(?:为|=|:|：)\s*" + _NUMBER, re.IGNORECASE),
    re.compile(r"(?:gpu|显卡)\s*" + _NUMBER + r"\s*(?:张|块|个)", re.IGNORECASE),
    re.compile(_NUMBER + r"\s*(?:张|块|个)\s*(?:gpu|显卡)", re.IGNORECASE),
    re.compile(r"(\d+)\s*(?:个)?\s*gpu\b", re.IGNORECASE),
)
_NO_GPU = re.compile(r"(?:不使用|无需|不要|无|不分配)\s*(?:gpu|显卡)", re.IGNORECASE)
_CREATE_INTENT = re.compile(r"创建|新建|启动|开|容器|create|start", re.IGNORECASE)


def requested_gpu_count(message):
    text = message or ""
    if _NO_GPU.search(text):
        return 0
    for pattern in _GPU_COUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1)
            if value.isdecimal():
                return int(value)
            digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                      "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
            if value in digits:
                return digits[value]
            if value == "十":
                return 10
            if value.count("十") == 1:
                tens, ones = value.split("十")
                if (not tens or tens in digits) and (not ones or ones in digits):
                    return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
    return None


def requests_zero_gpu_container(message):
    text = message or ""
    return bool(_CREATE_INTENT.search(text) and requested_gpu_count(text) == 0)


def zero_gpu_count(value):
    if isinstance(value, bool) or value is None:
        return False
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return False
