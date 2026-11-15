# -*- coding: utf-8 -*-
"""片名匹配工具。

文件名里常混有罗马音、英文译名、中文译名、季集号和压制标签。这里用完整
词元而不是字符串子串做匹配，避免 ``Yi`` 命中 ``flying``、``Star`` 命中
``Starring`` 这类误判；有中文时再补一层拼音比较，兼容罗马音文件名。
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Set

try:
    from pypinyin import lazy_pinyin

    PYPINYIN_AVAILABLE = True
except ImportError:
    lazy_pinyin = None
    PYPINYIN_AVAILABLE = False


_TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "was",
    "were",
    "with",
}

_TITLE_NOISE = re.compile(
    r"^(?:"
    r"s\d{1,2}(?:e\d{1,4})?|e\d{1,4}|(?:19|20)\d{2}|"
    r"\d{3,4}[pi]|4k|8k|web(?:dl|rip)?|bluray|bdrip|hdtv|remux|"
    r"h26[45]|hevc|avc|x26[45]|av1|ddp?\d?|aac|ac3|eac3|dts|"
    r"hdr10?|dv|sdr|10bit|8bit"
    r")$",
    re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )


def normalize_title(value: object) -> str:
    """归一化片名：忽略大小写、重音、标点和空格。"""
    text = _strip_accents(str(value or "")).lower().replace("&", " and ")
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", "", text)


def title_tokens(value: object) -> Set[str]:
    """按英文单词 / 连续 CJK 切分片名，并去掉冠词、季集号和压制标签。"""
    text = _strip_accents(str(value or "")).lower().replace("&", " and ")
    raw_tokens = re.findall(
        r"[0-9a-z]+|[\u3040-\u30ff]+|[\u3400-\u9fff]+|[\uac00-\ud7af]+",
        text,
    )
    return {
        token
        for token in raw_tokens
        if token not in _TITLE_STOPWORDS and not _TITLE_NOISE.match(token)
    }


def _ordered_title_tokens(value: object) -> List[str]:
    """按出现顺序返回词元，供英文缩写（SVU -> Special Victims Unit）匹配。"""
    text = _strip_accents(str(value or "")).lower().replace("&", " and ")
    raw_tokens = re.findall(
        r"[0-9a-z]+|[\u3040-\u30ff]+|[\u3400-\u9fff]+|[\uac00-\ud7af]+",
        text,
    )
    return [
        token
        for token in raw_tokens
        if token not in _TITLE_STOPWORDS and not _TITLE_NOISE.match(token)
    ]


def _has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", value or ""))


def _pinyin_title(value: object) -> str:
    """把含中文的标题转成连续拼音；没有 pypinyin 或纯英文时返回空串。"""
    text = str(value or "")
    if not PYPINYIN_AVAILABLE or not _has_cjk(text):
        return ""
    try:
        parts = lazy_pinyin(text, errors="ignore")
    except Exception:
        return ""
    return normalize_title(" ".join(parts))


def _has_uppercase_latin_token(value: object, token: str) -> bool:
    """标题里是否以全大写形式出现过某个英文词元（VIVANT / NCIS）。"""
    if not token or not token.isascii():
        return False
    return any(
        part.lower() == token and part.isascii() and part.isupper() and len(part) >= 2
        for part in re.findall(r"[A-Za-z]+", str(value or ""))
    )


def _acronym_matches(token: str, candidate_words: List[str]) -> bool:
    """缩写是否匹配候选标题中一段连续单词的首字母。"""
    target = str(token or "").lower()
    if len(target) < 3 or not target.isalpha():
        return False
    for start in range(len(candidate_words)):
        letters = ""
        for word in candidate_words[start:]:
            if not word.isalpha():
                continue
            letters += word[0]
            if letters == target:
                return True
            if len(letters) >= len(target) or not target.startswith(letters):
                break
    return False


def _acronym_query_matches(
    query_text: object,
    query_tokens: Set[str],
    candidate_text: object,
    candidate_tokens: Set[str],
) -> bool:
    """展开搜索词里的全大写缩写：Law and Order SVU -> Special Victims Unit。"""
    unmatched = query_tokens - candidate_tokens
    acronyms = [
        token
        for token in unmatched
        if len(token) >= 3 and _has_uppercase_latin_token(query_text, token)
    ]
    if not acronyms:
        return False
    if not (query_tokens - set(acronyms)).issubset(candidate_tokens):
        return False
    candidate_words = _ordered_title_tokens(candidate_text)
    return all(_acronym_matches(token, candidate_words) for token in acronyms)


def title_match_score(candidate: object, query: object) -> int:
    """候选片名与搜索词的匹配分；0 表示不能视为同一部作品。"""
    candidate_text = str(candidate or "")
    query_text = str(query or "")
    left = normalize_title(candidate_text)
    right = normalize_title(query_text)
    if not left or not right:
        return 0
    if left == right:
        return 100

    # 中文 / 日文标题与罗马音文件名：一路前行 <-> Yi Lu Qian Xing。
    if _has_cjk(candidate_text) != _has_cjk(query_text):
        candidate_pinyin = _pinyin_title(candidate_text)
        query_pinyin = _pinyin_title(query_text)
        latin_side = right if _has_cjk(candidate_text) else left
        cjk_pinyin = candidate_pinyin if _has_cjk(candidate_text) else query_pinyin
        if cjk_pinyin and cjk_pinyin == latin_side:
            return 95

    candidate_tokens = title_tokens(candidate_text)
    query_tokens = title_tokens(query_text)
    if not candidate_tokens or not query_tokens:
        return 0
    if candidate_tokens == query_tokens:
        return 90

    common = candidate_tokens & query_tokens

    if common and query_tokens.issubset(candidate_tokens):
        # 搜索词只有一个较短英文词时，仅允许候选里还有中文上下文
        # （例如 VIVANT -> 成吉思-VIVANT THE ORIGIN STORY-）。
        if len(query_tokens) == 1 and _has_cjk(candidate_text):
            return 70
        # 候选比搜索词多出副标题时不能自动认定：Star Wars 与
        # Star Wars: The Clone Wars 是不同作品，交给用户手动选更安全。
        # 全大写缩写也不能直接放行：NCIS 与 NCIS: Sydney、FBI 与
        # FBI: Most Wanted 都是不同作品；真正的缩写展开由下面的
        # _acronym_query_matches 处理。
        return 0

    if common and candidate_tokens.issubset(query_tokens):
        # 反方向同样要保守：搜索词多出副标题时，较短的候选可能是另一部
        # 作品（Star Wars / The Office US）。只有中文短标题允许作为主标题
        # 命中「中文名 + 英文副标题」的文件名。
        if candidate_tokens and all(
            _has_cjk(token) and len(token) >= 2 for token in candidate_tokens
        ):
            return 78
        return 0

    if _acronym_query_matches(
        query_text, query_tokens, candidate_text, candidate_tokens
    ):
        return 80

    if not common:
        return 0

    # 只共享一个词元时不能自动认定：Star / Starring、Your / Your Own Body 都
    # 很容易撞车。只有该词元足够长且是搜索词的核心时才放行。
    if len(common) == 1 and len(query_tokens) == 1:
        token = next(iter(common))
        if len(token) >= 7 and token in candidate_tokens:
            return 65
    return 0


def title_matches(candidate: object, query: object) -> bool:
    """候选片名是否与搜索词相关。"""
    return title_match_score(candidate, query) > 0
