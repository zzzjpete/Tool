"""WBI signing for Bilibili web API.

Bilibili gates many newer endpoints (search, user space, etc.) behind a
signature called `w_rid`, derived from a salt that's reshuffled from the
img/sub keys advertised by /x/web-interface/nav.
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Mapping

# Index permutation used by Bilibili's web client to derive the mixin key.
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18,  2, 53,  8, 23, 32,
    15, 50, 10, 31, 58,  3, 45, 35,
    27, 43,  5, 49, 33,  9, 42, 19,
    29, 28, 14, 39, 12, 38, 41, 13,
    37, 48,  7, 16, 24, 55, 40, 61,
    26, 17,  0,  1, 60, 51, 30,  4,
    22, 25, 54, 21, 56, 59,  6, 63,
    57, 62, 11, 36, 20, 34, 44, 52,
]


def get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def sign_params(params: Mapping[str, object], img_key: str, sub_key: str) -> dict:
    """Return params + wts + w_rid signed using img/sub keys."""
    mixin_key = get_mixin_key(img_key + sub_key)
    wts = int(time.time())
    signed = dict(params)
    signed["wts"] = wts

    # Sort, drop characters Bilibili strips, then md5.
    cleaned = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in signed.items()
    }
    query = urllib.parse.urlencode(sorted(cleaned.items()))
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


def extract_keys_from_nav(nav_data: Mapping) -> tuple[str, str]:
    """Pull (img_key, sub_key) from /x/web-interface/nav response data."""
    wbi = nav_data.get("wbi_img", {})
    img_url = wbi.get("img_url", "")
    sub_url = wbi.get("sub_url", "")
    img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
    return img_key, sub_key
