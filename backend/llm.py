"""LLM 客户端：翻译功能的后端代理。

设计要点：
- 配置存在 Setting 键值表里，键名统一加 ``llm_`` 前缀。api_key 明文存本地库，
  但对外 API 只返回是否已配置、不回传密钥（见 main.py 的 /api/settings）。
- 支持两种规范：
    * openai    -> POST {base_url}/chat/completions，Authorization: Bearer <key>
    * anthropic -> POST {base_url}/v1/messages，x-api-key + anthropic-version 头
  两者都是「兼容接口」，用户填自己的 base_url（官方或任意兼容网关）即可。
- 翻译分段对照：前端抽出正文的段落数组，后端要求模型逐段翻译并原样返回 JSON 数组，
  数量必须与输入一致；不一致时对超长/异常批次逐段兜底，保证对齐。
"""
from __future__ import annotations

import json
from typing import Optional

import httpx
from sqlmodel import Session, select

from backend.db import engine
from backend.models import Setting

# ---------- 配置读写 ----------
_PREFIX = "llm_"
_KEYS = ("api_type", "base_url", "api_key", "model")
_DEFAULTS = {"api_type": "openai", "base_url": "", "api_key": "", "model": ""}


def get_config() -> dict:
    """读取当前 LLM 配置，返回含全部键的 dict（缺失用默认值补齐）。"""
    cfg = dict(_DEFAULTS)
    with Session(engine) as s:
        rows = s.exec(select(Setting).where(Setting.key.like(f"{_PREFIX}%"))).all()
        for r in rows:
            k = r.key[len(_PREFIX):]
            if k in _DEFAULTS:
                cfg[k] = r.value
    return cfg


def save_config(data: dict) -> dict:
    """保存配置。只接受白名单键。api_key 为空字符串时表示"不修改"，避免前端掩码回写冲掉密钥。"""
    from datetime import datetime

    with Session(engine) as s:
        for k in _KEYS:
            if k not in data:
                continue
            val = data[k]
            if val is None:
                continue
            val = str(val).strip()
            # 密钥留空 = 保持原值不动（前端不回传明文时）
            if k == "api_key" and val == "":
                continue
            row = s.get(Setting, _PREFIX + k)
            if row is None:
                row = Setting(key=_PREFIX + k, value=val)
            else:
                row.value = val
                row.updated_at = datetime.utcnow()
            s.add(row)
        s.commit()
    return get_config()


def is_configured(cfg: Optional[dict] = None) -> bool:
    cfg = cfg or get_config()
    return bool(cfg.get("base_url") and cfg.get("api_key") and cfg.get("model"))


# ---------- 底层聊天调用 ----------
class LLMError(Exception):
    """对外抛出的可读错误。"""


def _normalize_base(base_url: str) -> str:
    return base_url.rstrip("/")


def chat(messages: list[dict], system: str = "", cfg: Optional[dict] = None,
         max_tokens: int = 4096, temperature: float = 0.2, timeout: float = 120.0) -> str:
    """按配置的 api_type 分派到 OpenAI 或 Anthropic 规范，返回模型文本。

    messages: [{"role": "user"/"assistant", "content": "..."}]，不含 system。
    system:   系统提示，单独传（Anthropic 用顶层 system 字段，OpenAI 拼进 messages）。
    """
    cfg = cfg or get_config()
    if not is_configured(cfg):
        raise LLMError("尚未配置模型接口，请先在设置里填写 base_url / api_key / model。")

    api_type = (cfg.get("api_type") or "openai").lower()
    base = _normalize_base(cfg["base_url"])
    key = cfg["api_key"]
    model = cfg["model"]

    if api_type == "anthropic":
        url = f"{base}/v1/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            payload["system"] = system
    else:  # openai 兼容
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "content-type": "application/json",
        }
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": msgs,
        }

    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMError(f"请求接口失败：{e}") from e

    if resp.status_code >= 400:
        detail = resp.text[:500]
        raise LLMError(f"接口返回 {resp.status_code}：{detail}")

    data = resp.json()
    try:
        if api_type == "anthropic":
            # content 是块数组，取所有 text 块拼接
            parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            return "".join(parts).strip()
        else:
            return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"解析接口响应失败：{e}；原始：{json.dumps(data)[:300]}") from e


def chat_stream(messages: list[dict], system: str = "", cfg: Optional[dict] = None,
                max_tokens: int = 2048, temperature: float = 0.3, timeout: float = 180.0):
    """流式聊天：按配置的 api_type 分派，逐块 yield 文本增量（str）。

    与 :func:`chat` 同构，但请求体带 ``stream: true``，用 SSE 解析增量：
    - openai:    每行 ``data: {json}``，取 choices[0].delta.content
    - anthropic: 事件流，取 content_block_delta 里的 delta.text
    出错时抛 :class:`LLMError`（生成器首次迭代时才会真正发起请求）。
    """
    cfg = cfg or get_config()
    if not is_configured(cfg):
        raise LLMError("尚未配置模型接口，请先在设置里填写 base_url / api_key / model。")

    api_type = (cfg.get("api_type") or "openai").lower()
    base = _normalize_base(cfg["base_url"])
    key = cfg["api_key"]
    model = cfg["model"]

    if api_type == "anthropic":
        url = f"{base}/v1/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
            "stream": True,
        }
        if system:
            payload["system"] = system
    else:  # openai 兼容
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "content-type": "application/json",
        }
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": msgs,
            "stream": True,
        }

    def _gen():
        try:
            with httpx.stream("POST", url, headers=headers, json=payload, timeout=timeout) as resp:
                if resp.status_code >= 400:
                    detail = resp.read().decode("utf-8", "replace")[:500]
                    raise LLMError(f"接口返回 {resp.status_code}：{detail}")
                for line in resp.iter_lines():
                    if not line:
                        continue
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if not data_str or data_str == "[DONE]":
                        if data_str == "[DONE]":
                            break
                        continue
                    try:
                        evt = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if api_type == "anthropic":
                        if evt.get("type") == "content_block_delta":
                            piece = (evt.get("delta") or {}).get("text", "")
                            if piece:
                                yield piece
                        elif evt.get("type") == "message_stop":
                            break
                    else:
                        choices = evt.get("choices") or []
                        if choices:
                            piece = (choices[0].get("delta") or {}).get("content", "")
                            if piece:
                                yield piece
        except httpx.HTTPError as e:
            raise LLMError(f"请求接口失败：{e}") from e

    return _gen()


# ---------- 翻译 ----------
_LANG_NAMES = {
    "zh": "简体中文", "zh-Hant": "繁體中文", "en": "English", "ja": "日本語",
    "ko": "한국어", "fr": "Français", "de": "Deutsch", "es": "Español",
    "ru": "Русский", "pt": "Português",
}

# 单批上限，控制单次请求体量，避免超长导致模型截断或对齐错乱
_BATCH_MAX_SEGS = 20
_BATCH_MAX_CHARS = 3500


def _translate_batch(segments: list[str], target_lang: str, cfg: dict) -> list[str]:
    """翻译一批段落，要求模型返回等长 JSON 数组。返回译文列表（与输入等长）。"""
    lang_name = _LANG_NAMES.get(target_lang, target_lang)
    system = (
        f"You are a professional translator. Translate each text segment into {lang_name}. "
        "Preserve the original meaning, tone and any inline formatting. "
        "Do NOT translate code, URLs, or proper nouns that should stay as-is. "
        "You will receive a JSON array of strings. Return ONLY a JSON array of the same length, "
        "where each element is the translation of the corresponding input element. "
        "No markdown, no explanations, no extra keys."
    )
    user = json.dumps(segments, ensure_ascii=False)
    raw = chat([{"role": "user", "content": user}], system=system, cfg=cfg,
               max_tokens=4096, temperature=0.2)

    out = _parse_json_array(raw)
    if out is not None and len(out) == len(segments):
        return [str(x) for x in out]

    # 对齐失败：逐段兜底翻译，保证一一对应
    return [_translate_single(seg, lang_name, cfg) for seg in segments]


def _translate_single(text: str, lang_name: str, cfg: dict) -> str:
    system = (
        f"Translate the user's text into {lang_name}. Preserve meaning and tone. "
        "Do not translate code or URLs. Return ONLY the translation, no quotes, no explanations."
    )
    try:
        return chat([{"role": "user", "content": text}], system=system, cfg=cfg,
                    max_tokens=2048, temperature=0.2)
    except LLMError:
        return ""  # 单段失败不整体崩，前端会显示原文


def _parse_json_array(raw: str) -> Optional[list]:
    """尽量从模型输出里抠出 JSON 数组。容忍 ```json 代码块包裹。"""
    s = raw.strip()
    if s.startswith("```"):
        # 去掉围栏
        s = s.split("```", 2)
        s = s[1] if len(s) > 1 else raw
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip()
        # 可能还带尾部围栏
        if s.endswith("```"):
            s = s[:-3].strip()
    l, r = s.find("["), s.rfind("]")
    if l == -1 or r == -1 or r < l:
        return None
    try:
        arr = json.loads(s[l:r + 1])
        return arr if isinstance(arr, list) else None
    except json.JSONDecodeError:
        return None


def translate_segments(segments: list[str], target_lang: str, cfg: Optional[dict] = None) -> list[str]:
    """翻译一组段落，返回等长译文列表。按字数/条数自动分批。"""
    cfg = cfg or get_config()
    if not is_configured(cfg):
        raise LLMError("尚未配置模型接口，请先在设置里填写 base_url / api_key / model。")
    if not segments:
        return []

    results: list[str] = []
    batch: list[str] = []
    batch_chars = 0
    for seg in segments:
        seg_len = len(seg)
        if batch and (len(batch) >= _BATCH_MAX_SEGS or batch_chars + seg_len > _BATCH_MAX_CHARS):
            results.extend(_translate_batch(batch, target_lang, cfg))
            batch, batch_chars = [], 0
        batch.append(seg)
        batch_chars += seg_len
    if batch:
        results.extend(_translate_batch(batch, target_lang, cfg))
    return results
