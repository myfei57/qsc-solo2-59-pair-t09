"""规范化摘要。

复核材料里每一份证据都要能被独立校验，摘要算法必须各处一致：先按键名排序
再序列化，避免字段书写顺序不同导致同一证据算出不同摘要。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_digest(payload: Any) -> str:
    """返回载荷的 SHA-256 摘要（十六进制）。"""

    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
