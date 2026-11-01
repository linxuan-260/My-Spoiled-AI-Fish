"""中文二元组切分。

为什么用 bigram 而不是 jieba：
    1. jieba 有歧义/词典依赖，换个人名就切错；bigram 无词典、召回稳定。
    2. FTS5 内置 tokenizer 不认中文（unicode61 会把整句当一个 token），
       所以入库和查询两边都先切成「空格分隔的 bigram」，再用 unicode61。
    3. 查询是 OR 语义 + bm25 排序，切错一两个不影响排序。
"""

from __future__ import annotations

import re
import unicodedata

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RUN = re.compile(rf"[{_CJK}]+")
_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_+#.\-]*|\d+(?:\.\d+)?")
_PUNCT = re.compile(r"[\s\u3000!-/:-@\[-`{-~，。！？、；：""''（）【】《》…—·~]+")

MAX_GRAMS = 64


def norm(text: str) -> str:
    """全角转半角 + 小写 + 去控制符。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch for ch in t if ch == "\n" or not unicodedata.category(ch).startswith("C"))
    return t.lower().strip()


def bigrams(text: str) -> list[str]:
    """切出 bigram 列表，顺序保留、去重、限长。"""
    t = norm(text)
    if not t:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def push(g: str) -> None:
        if g and g not in seen:
            seen.add(g)
            out.append(g)

    for run in _CJK_RUN.findall(t):
        if len(run) == 1:
            push(run)
            continue
        for i in range(len(run) - 1):
            push(run[i : i + 2])

    for w in _WORD.findall(t):
        push(w)

    # 数字/英文往往单独有用，CJK 单字也补一遍提升召回
    for run in _CJK_RUN.findall(t):
        if len(run) >= 3:
            for ch in run:
                push(ch)

    return out[:MAX_GRAMS]


def grams_text(text: str) -> str:
    """入库用：空格拼接的 bigram 串。"""
    return " ".join(bigrams(text))


def fts_query(text: str, limit: int = 32) -> str:
    """查询用：带引号的 OR 表达式。空 -> 空串。"""
    gs = bigrams(text)
    if not gs:
        return ""
    qs = []
    for g in gs[:limit]:
        g = g.replace('"', '""')
        qs.append(f'"{g}"')
    return " OR ".join(qs)


def strip_punct(text: str) -> str:
    return _PUNCT.sub("", norm(text))


def is_chinese(ch: str) -> bool:
    return bool(re.match(rf"[{_CJK}]", ch))
