#!/usr/bin/env python3
"""Apply two surgical fixes and back up every touched file.

1) astrbot/core/provider/sources/openai_source.py
   _image_ref_to_data_url now normalizes image bytes (GIF/WEBP/AVIF/BMP or
   oversized payloads) through the shared model-image helper before they are
   sent to a multimodal provider. Fixes Zhipu 400 "image format/parse error".

2) data/plugins/astrbot_plugin_self_evolution/engine/session_memory_store.py
   retrieve_summary returns early on an empty query, so the knowledge-base
   embedding call is never made with empty input (Zhipu 400).
"""
import sys
import time
from pathlib import Path

TS = time.strftime("%Y%m%d_%H%M%S")
SP = Path("/opt/astrbot/.venv/lib/python3.12/site-packages")
OPENAI_SRC = SP / "astrbot/core/provider/sources/openai_source.py"
SESS_STORE = Path(
    "/opt/astrbot/data/plugins/astrbot_plugin_self_evolution/engine/session_memory_store.py"
)

failures = []


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def backup(p: Path) -> Path:
    b = p.with_name(p.name + ".bak-imgfix-" + TS)
    if not b.exists():
        b.write_text(read(p), encoding="utf-8")
    return b


def replace_once(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        failures.append("%s: expected 1 match, found %d" % (label, n))
        return text
    return text.replace(old, new, 1)


# ---------------------------------------------------------------- fix 1
old_tail = "        return image_data.to_data_url() if image_data else None\n"
new_tail = (
    "        if not image_data:\n"
    "            return None\n"
    "        return self._prepare_image_data_url(image_data)\n"
)

helper = '''    def _prepare_image_data_url(self, image_data):
        """Return a data URL whose bytes and MIME type a provider can read.

        Some platforms hand over GIF/WEBP/AVIF payloads under a ``.jpg`` name, and
        providers such as Zhipu reject them with HTTP 400 (image format/parse
        error). Sniff the real bytes, keep compliant JPEG/PNG payloads untouched,
        and otherwise transcode through the shared model-image helper. Never
        raise: fall back to the original payload so one bad image cannot fail the
        whole request.
        """
        import base64

        try:
            from astrbot.core.utils.media_utils import (
                MODEL_IMAGE_MAX_BYTES,
                ResolvedMediaData,
                _prepare_model_image_sync,
                detect_image_mime_type,
            )

            raw = image_data.to_bytes()
            mime = detect_image_mime_type(raw) or image_data.mime_type
            if mime in {"image/jpeg", "image/png"} and len(raw) < MODEL_IMAGE_MAX_BYTES:
                if mime == image_data.mime_type:
                    return image_data
                return ResolvedMediaData(
                    base64_data=image_data.base64_data,
                    mime_type=mime,
                )
            prepared, _is_montage = _prepare_model_image_sync(raw, 1280)
            prepared_mime = detect_image_mime_type(prepared) or "image/jpeg"
            logger.debug(
                "Normalized provider image: %s %d bytes -> %s %d bytes",
                mime,
                len(raw),
                prepared_mime,
                len(prepared),
            )
            return ResolvedMediaData(
                base64_data=base64.b64encode(prepared).decode("ascii"),
                mime_type=prepared_mime,
            )
        except Exception as exc:
            logger.warning(
                "Image normalization failed, sending original payload: %s", exc
            )
            return image_data

'''

text = read(OPENAI_SRC)
if "def _prepare_image_data_url" in text:
    print("[skip] openai_source.py already patched")
else:
    text2 = replace_once(text, old_tail, new_tail, "openai_source.py: return tail")
    anchor = "    async def _resolve_image_part(\n"
    text2 = replace_once(text2, anchor, helper + anchor, "openai_source.py: helper anchor")
    if not failures:
        backup(OPENAI_SRC)
        OPENAI_SRC.write_text(text2, encoding="utf-8")
        print("[ok] patched", OPENAI_SRC)

# ---------------------------------------------------------------- fix 2
GUARD = (
    "        query = str(query or \"\").strip()\n"
    "        if not query:\n"
    "            logger.debug(\"[MemoryStore] retrieve_summary skipped: empty query\")\n"
    "            return \"\"\n"
)

if SESS_STORE.exists():
    st = read(SESS_STORE)
    if "retrieve_summary skipped: empty query" in st:
        print("[skip] session_memory_store.py already patched")
    else:
        marker = "    async def retrieve_summary(\n"
        idx = st.find(marker)
        if idx < 0:
            failures.append("retrieve_summary not found")
        else:
            try_idx = st.find("\n        try:\n", idx)
            if try_idx < 0:
                failures.append("retrieve_summary try-block not found")
            else:
                insert_at = try_idx + 1
                st2 = st[:insert_at] + GUARD + st[insert_at:]
                if not failures:
                    backup(SESS_STORE)
                    SESS_STORE.write_text(st2, encoding="utf-8")
                    print("[ok] patched", SESS_STORE)
else:
    failures.append("session_memory_store.py missing")

# ---------------------------------------------------------------- syntax check
import py_compile

for p in (OPENAI_SRC, SESS_STORE):
    try:
        py_compile.compile(str(p), doraise=True)
        print("[compile ok]", p.name)
    except Exception as exc:
        failures.append("compile failed %s: %s" % (p.name, exc))

if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL GOOD")

