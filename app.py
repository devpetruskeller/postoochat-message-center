from __future__ import annotations

import json
import io
import hmac
import html
import mimetypes
import os
import re
import threading
from copy import deepcopy
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "messages.json"
EXPORT_PAYLOAD_PATH = BASE_DIR / "messages.export.json"
COLLECTORS_PATH = BASE_DIR / "collectors.json"
ENV_PATHS = [BASE_DIR / ".env"]
PUBLIC_DIR = BASE_DIR / "public"
HOST = "127.0.0.1"
PORT = 8765
ALLOWED_ASSET_TYPES = {"image", "audio", "video", "document", "sticker"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
COLLECTORS_LOCK = threading.Lock()


class MultipartUpload:
    def __init__(self, filename: str, content_type: str, content: bytes) -> None:
        self.filename = filename
        self.type = content_type
        self.file = io.BytesIO(content)


class MultipartForm:
    def __init__(self) -> None:
        self.fields: dict[str, str | MultipartUpload] = {}

    def getfirst(self, name: str, default: str = "") -> str:
        value = self.fields.get(name, default)
        return value if isinstance(value, str) else default

    def __contains__(self, name: str) -> bool:
        return name in self.fields

    def __getitem__(self, name: str) -> str | MultipartUpload:
        return self.fields[name]


def normalize_taxonomy(raw_taxonomy: Any, messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    taxonomy: dict[str, set[str]] = {}
    if isinstance(raw_taxonomy, dict):
        for raw_group, raw_categories in raw_taxonomy.items():
            group = slugify(raw_group)
            if not group:
                continue
            taxonomy[group] = {str(category or "").strip() for category in raw_categories if isinstance(category, str)} \
                if isinstance(raw_categories, list) else {""}

    for message in messages:
        group = slugify(message.get("group", "postoochat") or "postoochat")
        category = str(message.get("category", "") or "").strip()
        taxonomy.setdefault(group, set()).add(category)

    for group in taxonomy:
        taxonomy[group].add("")

    # Preserve folders already used by the PosToo groups when upgrading catalogs
    # that predate persistent taxonomy. Once saved, users can edit/remove them normally.
    if not isinstance(raw_taxonomy, dict):
        for group in ("postoochat", "postoo_chat"):
            if group in taxonomy:
                taxonomy[group].add("templates")

    return {
        group: sorted(categories, key=lambda value: (value != "", value))
        for group, categories in sorted(taxonomy.items())
    }


def build_suite_app_directory(taxonomy: dict[str, list[str]]) -> list[dict[str, str]]:
    """Derive safe app-picker metadata from Message Center group names."""
    labels = {
        "chatcenter": "ChatCenter",
        "faceluv": "FaceLuv",
        "market": "Market",
        "ride": "Ride",
    }
    apps: list[dict[str, str]] = []
    for group in sorted(taxonomy):
        if not group.startswith("postoochat_") or group == "postoochat_suite":
            continue
        app_id = group.removeprefix("postoochat_")
        if not app_id:
            continue
        apps.append({
            "app_id": app_id,
            "label": labels.get(app_id, app_id.replace("_", " ").title()),
            "message_group": group,
            "onboarding": "suite",
            "availability": "under_construction",
        })
    return apps


def load_catalog() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return {"messages": [], "taxonomy": {}}
    catalog = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    messages = [normalize_message_shape(message) for message in catalog.get("messages", []) if isinstance(message, dict)]
    return {
        "messages": messages,
        "taxonomy": normalize_taxonomy(catalog.get("taxonomy"), messages),
    }


def save_catalog(catalog: dict[str, Any]) -> None:
    messages = [normalize_message_shape(message) for message in catalog.get("messages", []) if isinstance(message, dict)]
    catalog = {
        "taxonomy": normalize_taxonomy(catalog.get("taxonomy"), messages),
        "messages": messages,
    }
    serialized = json.dumps(catalog, indent=2, ensure_ascii=True) + "\n"
    DATA_PATH.write_text(serialized, encoding="utf-8")
    save_export_payload(build_export_payload(catalog))
    sync_collectors(catalog["taxonomy"])


def find_message(catalog: dict[str, Any], name: str) -> dict[str, Any] | None:
    for message in catalog.get("messages", []):
      if message.get("name") == name:
          return message
    return None


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    return override if override is not None else base


def interpolate(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"{{\s*([a-zA-Z0-9_]+)\s*}}", lambda match: variables.get(match.group(1), ""), value)
    if isinstance(value, list):
        return [interpolate(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: interpolate(item, variables) for key, item in value.items()}
    return value


def normalize_block_id(message_name: str, index: int, block: dict[str, Any]) -> str:
    existing = str(block.get("id", "")).strip()
    if existing:
        return existing
    text = str(block.get("text", "")).lower()
    if "reply with any number from numbered-menu" in text:
        return "numbered_menu"
    safe_name = re.sub(r"[^a-z0-9]+", "_", message_name.lower()).strip("_") or "message"
    return f"{safe_name}_block_{index + 1}"


def normalize_blocks(message_name: str, blocks: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw_block in enumerate(blocks if isinstance(blocks, list) else []):
        if not isinstance(raw_block, dict):
            continue
        block = dict(raw_block)
        block["id"] = normalize_block_id(message_name, index, block)
        if block.get("type") == "text" and not isinstance(block.get("required"), bool):
            block["required"] = True
        normalized.append(block)
    return normalized


def normalize_body_block(message_name: str, body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    normalized = normalize_blocks(message_name, [{**body, "id": str(body.get("id") or "body")}])
    if not normalized:
        return None
    block = normalized[0]
    if block.get("type") == "text" and not isinstance(block.get("required"), bool):
        block["required"] = True
    return block


def merge_blocks(message_name: str, base_blocks: Any, override_blocks: Any) -> list[dict[str, Any]]:
    base = normalize_blocks(message_name, base_blocks)
    override = normalize_blocks(message_name, override_blocks)
    if not override:
        return base
    if not base:
        return override

    merged: list[dict[str, Any]] = []
    base_by_id = {str(block.get("id", "")): block for block in base}
    used_ids: set[str] = set()

    for override_block in override:
        block_id = str(override_block.get("id", ""))
        if block_id and block_id in base_by_id:
            merged.append(deep_merge(base_by_id[block_id], override_block))
            used_ids.add(block_id)
        else:
            merged.append(override_block)

    for base_block in base:
        block_id = str(base_block.get("id", ""))
        if block_id in used_ids:
            continue
        merged.append(base_block)
    return merged


def normalize_message_shape(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    normalized = dict(message)
    message_name = str(normalized.get("name", "message"))
    normalized["group"] = slugify(normalized.get("group", "postoochat") or "postoochat")
    normalized["category"] = str(normalized.get("category", "") or "").strip()

    default_variant = dict(normalized.get("default", {}))
    normalized_body = normalize_body_block(message_name, default_variant.get("body"))
    if normalized_body:
        default_variant["body"] = normalized_body
    else:
        default_variant.pop("body", None)
    default_variant["blocks"] = normalize_blocks(message_name, default_variant.get("blocks", []))
    normalized["default"] = default_variant

    overrides: dict[str, Any] = {}
    for channel, raw_override in dict(normalized.get("overrides", {})).items():
        if not isinstance(raw_override, dict):
            continue
        override = dict(raw_override)
        normalized_body = normalize_body_block(message_name, override.get("body"))
        if normalized_body:
            override["body"] = normalized_body
        else:
            override.pop("body", None)
        if "blocks" in override:
            override["blocks"] = normalize_blocks(message_name, override.get("blocks", []))
        overrides[channel] = override
    normalized["overrides"] = overrides
    normalized["assets"] = normalize_assets(normalized.get("assets", []))
    normalized["links"] = normalize_links(normalized.get("links", []))
    return normalized


def slugify(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_") or "message"


def normalize_asset_type(value: Any) -> str:
    asset_type = str(value or "").strip().lower()
    if asset_type not in ALLOWED_ASSET_TYPES:
        raise ValueError("invalid_asset_type")
    return asset_type


def public_asset_root_setting() -> str:
    return get_setting("PT_MESSAGE_STUDIO_IMAGES", "public/message-images").strip() or "public/message-images"


def normalize_asset_storage_subpath() -> str:
    configured = public_asset_root_setting().replace("\\", "/").strip().strip("/")
    if configured.startswith("public/"):
        configured = configured[len("public/"):]
    elif configured == "public":
        configured = ""
    return configured.strip("/")


def asset_storage_dir() -> Path:
    configured = public_asset_root_setting().replace("\\", "/").strip().strip("/")
    relative = Path(configured or "public")
    candidate = (BASE_DIR / relative).resolve()
    try:
        candidate.relative_to(PUBLIC_DIR.resolve())
        return candidate
    except ValueError:
        return (PUBLIC_DIR / "message-images").resolve()


def normalize_asset_path(path: Any) -> str:
    value = str(path or "").strip().replace("\\", "/").strip("/")
    if not value:
        return ""
    if re.match(r"^[a-zA-Z]+://", value) or value.startswith("localhost") or re.match(r"^[a-zA-Z]:", value):
        return ""
    public_prefix = normalize_asset_storage_subpath()
    if value.startswith("public/"):
        value = value[len("public/"):]
    if public_prefix and value.startswith(public_prefix + "/"):
        return value
    filename = value.split("/")[-1]
    return f"{public_prefix}/{filename}".strip("/") if public_prefix else filename


def normalize_asset_key(value: Any, path: Any = "", asset_type: Any = "") -> str:
    raw = str(value or "").strip()
    if raw:
        return slugify(raw)
    normalized_path = normalize_asset_path(path)
    stem = Path(normalized_path).stem if normalized_path else ""
    if stem:
        return slugify(stem)
    return slugify(asset_type or "asset")


def normalize_assets(assets: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_asset in assets if isinstance(assets, list) else []:
        if not isinstance(raw_asset, dict):
            continue
        asset_type = str(raw_asset.get("type", "")).strip().lower()
        if asset_type not in ALLOWED_ASSET_TYPES:
            continue
        asset_url = str(raw_asset.get("url", "")).strip()
        if not re.match(r"^https://", asset_url, flags=re.IGNORECASE):
            continue
        asset_id = normalize_asset_key(
            raw_asset.get("assetId") or raw_asset.get("key"),
            "",
            asset_type,
        )
        asset: dict[str, Any] = {
            "assetId": asset_id,
            "type": asset_type,
            "url": asset_url,
            "live": raw_asset.get("live", True) is not False,
            "required": raw_asset.get("required", False) is True,
        }
        alt = str(raw_asset.get("alt", "")).strip()
        if alt:
            asset["alt"] = alt
        normalized.append(asset)
    return normalized


def normalize_links(links: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw_link in links if isinstance(links, list) else []:
        if not isinstance(raw_link, dict):
            continue
        variable = str(raw_link.get("variable", "")).strip()
        if not variable:
            continue
        link: dict[str, Any] = {
            "variable": variable,
            "live": raw_link.get("live", True) is not False,
            "required": raw_link.get("required", False) is True,
            "mode": "follow_up",
        }
        key = str(raw_link.get("key", "")).strip()
        if key:
            link["key"] = key
        normalized.append(link)
    return normalized


def build_asset_url(relative_path: str) -> str:
    normalized_path = normalize_asset_path(relative_path)
    if not normalized_path:
        return ""
    public_url = get_setting("PT_MESSAGE_STUDIO_PUBLIC_URL").strip().rstrip("/")
    storage_prefix = normalize_asset_storage_subpath()
    suffix = normalized_path
    if storage_prefix and suffix.startswith(storage_prefix + "/"):
        suffix = suffix[len(storage_prefix) + 1:]
    if public_url:
        return f"{public_url}/{suffix}".rstrip("/")
    return f"/public/{normalized_path}"


def hydrate_assets(assets: Any) -> list[dict[str, Any]]:
    return [dict(asset) for asset in normalize_assets(assets)]


def hydrate_message(message: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_message_shape(message)
    hydrated = deepcopy(normalized)
    hydrated["assets"] = hydrate_assets(normalized.get("assets", []))
    return hydrated


def detect_extension(filename: str, content_type: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix or ""):
        suffix = ""
    if suffix == ".jpeg":
        return ".jpg"
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower())
    if guessed == ".jpeg":
        return ".jpg"
    if guessed and re.fullmatch(r"\.[a-z0-9]{1,10}", guessed):
        return guessed
    return ""


def build_asset_filename(message: dict[str, Any], extension: str) -> str:
    message_id = str(message.get("id", "")).strip() or "message"
    message_slug = slugify(message.get("name", "message"))
    safe_extension = extension if re.fullmatch(r"\.[a-z0-9]{1,10}", extension or "") else ""
    return f"{message_id}_{message_slug}{safe_extension}"


def default_asset_alt(message: dict[str, Any], asset_type: str) -> str:
    label = str(message.get("name", "message")).replace("_", " ").strip().title() or "Message"
    return f"{label} {asset_type}"


def default_asset_key(original_name: str, asset_type: str) -> str:
    stem = Path(original_name or "").stem
    return normalize_asset_key(stem, "", asset_type)


def upsert_message_asset(
    message: dict[str, Any],
    asset_key: str,
    asset_type: str,
    relative_path: str,
    alt: str,
    live: bool = True,
    required: bool = False,
) -> dict[str, Any]:
    normalized = normalize_message_shape(message)
    existing_assets = normalized.get("assets", [])
    updated_assets: list[dict[str, Any]] = []
    replaced = False
    for existing in existing_assets:
        if not isinstance(existing, dict):
            continue
        existing_path = normalize_asset_path(existing.get("path"))
        existing_type = str(existing.get("type", "")).strip().lower()
        existing_key = normalize_asset_key(existing.get("key"), existing_path, existing_type)
        if existing_path == relative_path or existing_key == asset_key:
            updated_assets.append({
                "key": asset_key,
                "type": asset_type,
                "path": relative_path,
                "alt": alt,
                "live": live,
                "required": required,
            })
            replaced = True
            continue
        updated_assets.append(existing)
    if not replaced:
        updated_assets.append({
            "key": asset_key,
            "type": asset_type,
            "path": relative_path,
            "alt": alt,
            "live": live,
            "required": required,
        })
    normalized["assets"] = normalize_assets(updated_assets)
    return normalized


def save_uploaded_asset(message_name: str, asset_type_value: str, upload: MultipartUpload, asset_key_value: str = "") -> dict[str, Any]:
    asset_type = normalize_asset_type(asset_type_value)
    catalog = load_catalog()
    message = find_message(catalog, message_name)
    if not message:
        raise FileNotFoundError("message_not_found")
    if not getattr(upload, "file", None):
        raise ValueError("file_required")

    original_name = str(getattr(upload, "filename", "") or "").strip()
    content_type = str(getattr(upload, "type", "") or "").strip().lower()
    extension = detect_extension(original_name, content_type)
    if not extension:
        raise ValueError("invalid_file_extension")

    storage_dir = asset_storage_dir()
    storage_dir.mkdir(parents=True, exist_ok=True)
    filename = build_asset_filename(message, extension)
    target_path = (storage_dir / filename).resolve()
    try:
        target_path.relative_to(storage_dir)
    except ValueError as error:
        raise ValueError("invalid_asset_path") from error

    file_bytes = upload.file.read()
    if not file_bytes:
        raise ValueError("empty_file")

    target_path.write_bytes(file_bytes)
    relative_prefix = normalize_asset_storage_subpath()
    relative_path = f"{relative_prefix}/{filename}".strip("/") if relative_prefix else filename
    alt = default_asset_alt(message, asset_type)
    asset_key = normalize_asset_key(asset_key_value, "", asset_type) if str(asset_key_value or "").strip() else default_asset_key(original_name, asset_type)
    updated_message = upsert_message_asset(message, asset_key, asset_type, relative_path, alt, live=True, required=False)

    messages = catalog.get("messages", [])
    for index, existing in enumerate(messages):
        if existing.get("name") == message_name:
            messages[index] = updated_message
            break
    save_catalog(catalog)

    return {
        "message": hydrate_message(updated_message),
        "asset": hydrate_assets([{
            "key": asset_key,
            "type": asset_type,
            "path": relative_path,
            "alt": alt,
            "live": True,
            "required": False,
        }])[0],
    }


def channel_uses_inline_actions(resolved: dict[str, Any]) -> bool:
    actions = resolved.get("actions", [])
    delivery = str(resolved.get("delivery", ""))
    return isinstance(actions, list) and len(actions) > 0 and delivery in {"inline_buttons", "interactive"}


def apply_channel_behavior(resolved: dict[str, Any], channel: str) -> dict[str, Any]:
    variant = dict(resolved)
    variant["body"] = dict(resolved.get("body", {})) if isinstance(resolved.get("body"), dict) else None
    variant["actions"] = list(resolved.get("actions", [])) if isinstance(resolved.get("actions"), list) else []
    variant["blocks"] = list(resolved.get("blocks", [])) if isinstance(resolved.get("blocks"), list) else []

    if channel == "telegram":
        inline_buttons_enabled = variant.get("inline_buttons_enabled", True) is not False
        if not inline_buttons_enabled:
            variant["delivery"] = "plain_text"
            variant["actions"] = []
        return variant

    template_enabled = variant.get("template_enabled", False) is True
    if not template_enabled:
        variant["delivery"] = "plain_text"
        variant["actions"] = []
    return variant


def should_render_block(block: dict[str, Any], resolved: dict[str, Any]) -> bool:
    if block.get("type") != "text":
        return True
    required = block.get("required")
    if isinstance(required, bool) and not required:
        return False
    if block.get("id") == "numbered_menu" and channel_uses_inline_actions(resolved):
        return False
    return True


def resolved_content_blocks(resolved: dict[str, Any], message_name: str) -> list[dict[str, Any]]:
    body_block = normalize_body_block(message_name, resolved.get("body"))
    blocks = normalize_blocks(message_name, resolved.get("blocks", []))
    content: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    if body_block and should_render_block(body_block, resolved):
        content.append(body_block)
        used_ids.add(str(body_block.get("id", "")))

    for block in blocks:
        block_id = str(block.get("id", ""))
        if block_id in used_ids:
            continue
        if should_render_block(block, resolved):
            content.append(block)
    return content


def resolve_message(message: dict[str, Any], channel: str, variables: dict[str, str]) -> dict[str, Any]:
    normalized_message = normalize_message_shape(message)
    default_variant = normalized_message.get("default", {})
    channel_override = normalized_message.get("overrides", {}).get(channel, {})
    resolved = deep_merge(default_variant, channel_override)
    override_body = channel_override.get("body")
    # A disabled body override means "use the shared body", rather than
    # producing a message with no primary content.
    if isinstance(override_body, dict) and override_body.get("required") is False:
        resolved["body"] = deepcopy(default_variant.get("body", {}))
    else:
        resolved["body"] = deep_merge(default_variant.get("body"), override_body)
    resolved["blocks"] = merge_blocks(
        str(normalized_message.get("name", "message")),
        default_variant.get("blocks", []),
        channel_override.get("blocks", []),
    )
    resolved = apply_channel_behavior(resolved, channel)
    resolved["blocks"] = resolved_content_blocks(resolved, str(normalized_message.get("name", "message")))
    return interpolate(resolved, variables)


def collect_formats(value: Any) -> list[str]:
    found: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            formats = node.get("format")
            if isinstance(formats, list):
                for item in formats:
                    if isinstance(item, str) and item not in found:
                        found.append(item)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip("'\"")
    return parsed


def get_setting(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]
    for path in ENV_PATHS:
        parsed = parse_env_file(path)
        if name in parsed:
            return parsed[name]
    return default


def apply_transforms(text: str, formats: list[str] | None = None) -> str:
    placeholders: list[str] = []

    def protect(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"__MSG_STUDIO_PLACEHOLDER_{len(placeholders) - 1}__"

    value = re.sub(r"{{\s*[a-zA-Z0-9_]+\s*}}", protect, text)
    format_list = formats or []
    if "uppercase" in format_list:
        value = value.upper()
    if "lowercase" in format_list:
        value = value.lower()
    if "capitalize" in format_list:
        value = re.sub(r"\b\w", lambda match: match.group(0).upper(), value)
    for index, placeholder in enumerate(placeholders):
        value = value.replace(f"__MSG_STUDIO_PLACEHOLDER_{index}__", placeholder)
    return value


def wrap_styled_text(
    text: str,
    formats: list[str] | None,
    channel: str,
    parse_mode: str = "",
    escape_html: bool = True,
) -> str:
    value = apply_transforms(text, formats)
    is_telegram_html = channel == "telegram" and parse_mode.lower() == "html"
    if is_telegram_html and escape_html:
        value = html.escape(value)
    # Telegram's Markdown delimiters cannot contain the trailing or leading
    # spaces that naturally occur between Composer text parts. Keep those
    # spaces outside the delimiters, for example `_*Mini-App*_ ` rather than
    # `_*Mini-App *_`, so Telegram renders the selected formatting.
    leading_whitespace = value[:len(value) - len(value.lstrip())]
    trailing_whitespace = value[len(value.rstrip()):]
    value = value.strip()
    if not value:
        return leading_whitespace + trailing_whitespace
    wrappers: list[tuple[str, str]] = []
    format_list = formats or []

    if "bold" in format_list:
        wrappers.append(("<b>", "</b>") if is_telegram_html else ("*", "*"))
    if "italic" in format_list:
        wrappers.append(("<i>", "</i>") if is_telegram_html else ("_", "_"))
    if "strike" in format_list:
        wrappers.append(("<s>", "</s>") if is_telegram_html else ("~", "~"))
    if "code" in format_list:
        wrappers.append(("<code>", "</code>") if is_telegram_html else ("`", "`"))
    if "quote" in format_list:
        value = f"> {value}"
    if "underline" in format_list and is_telegram_html:
        wrappers.append(("<u>", "</u>"))
    elif "underline" in format_list and channel == "whatsapp":
        wrappers.append(("_", "_"))
    if "spoiler" in format_list:
        wrappers.append(("||", "||"))

    for prefix, suffix in wrappers:
        value = f"{prefix}{value}{suffix}"
    return f"{leading_whitespace}{value}{trailing_whitespace}"


def interpolate_text(text: str, variables: dict[str, str]) -> str:
    return re.sub(r"{{\s*([a-zA-Z0-9_]+)\s*}}", lambda match: variables.get(match.group(1), ""), text)


def render_block_text(block: dict[str, Any], variables: dict[str, str], channel: str, parse_mode: str = "") -> str:
    if isinstance(block.get("parts"), list) and block.get("parts"):
        parts: list[str] = []
        for part in block.get("parts", []):
            if not isinstance(part, dict):
                continue
            interpolated = interpolate_text(str(part.get("text", "")), variables)
            parts.append(wrap_styled_text(interpolated, part.get("format"), channel, parse_mode))
        return wrap_styled_text(
            "".join(parts), block.get("format"), channel, parse_mode, escape_html=False
        )

    interpolated = interpolate_text(str(block.get("text", "")), variables)
    return wrap_styled_text(interpolated, block.get("format"), channel, parse_mode)


def merge_variant(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    return {
        **(base or {}),
        **(override or {}),
        "binding": {
            **((base or {}).get("binding", {}) if isinstance((base or {}).get("binding", {}), dict) else {}),
            **((override or {}).get("binding", {}) if isinstance((override or {}).get("binding", {}), dict) else {}),
        },
        "body": deep_merge((base or {}).get("body"), (override or {}).get("body")),
        "blocks": (override or {}).get("blocks") if isinstance((override or {}).get("blocks"), list) else (base or {}).get("blocks", []),
        "actions": (override or {}).get("actions") if isinstance((override or {}).get("actions"), list) else (base or {}).get("actions", []),
    }


def merge_channel_variant(message: dict[str, Any], channel: str) -> dict[str, Any]:
    default_variant = dict(message.get("default", {}))
    override = dict(message.get("overrides", {})).get(channel)
    merged = merge_variant(default_variant, override)
    # An empty channel block list means "inherit the shared blocks".  Preserve
    # the same block merge semantics used by the editor preview and runtime
    # resolver so exported WhatsApp text does not lose every block after body.
    merged["blocks"] = merge_blocks(
        str(message.get("name", "message")),
        default_variant.get("blocks", []),
        (override or {}).get("blocks", []),
    )
    return merged


def build_export_variables(message: dict[str, Any], channel_variant: dict[str, Any]) -> dict[str, str]:
    exported: dict[str, str] = {}
    title = str(channel_variant.get("title", "")).strip()
    if title:
        exported["title"] = title
    for name in dict(message.get("variables", {})).keys():
        exported[str(name)] = f"{{{{{name}}}}}"
    return exported


def build_rendered_result(message: dict[str, Any], channel: str) -> tuple[dict[str, str], str]:
    channel_variant = apply_channel_behavior(merge_channel_variant(message, channel), channel)
    parse_mode = str(channel_variant.get("parse_mode", "") or "")
    visible_blocks = resolved_content_blocks(channel_variant, str(message.get("name", "message")))
    export_variables = build_export_variables(message, channel_variant)
    placeholder_variables = {name: f"{{{{{name}}}}}" for name in export_variables}

    lines: list[str] = []
    for block in visible_blocks:
        rendered = render_block_text(block, placeholder_variables, channel, parse_mode)
        if rendered.strip():
            lines.append(rendered)
    return export_variables, "\n\n".join(lines)


def build_export_channel_fields(message: dict[str, Any], channel: str) -> dict[str, Any]:
    channel_variant = apply_channel_behavior(merge_channel_variant(message, channel), channel)
    exported: dict[str, Any] = {}

    if isinstance(channel_variant.get("title"), str) and str(channel_variant.get("title")).strip():
        exported["title"] = channel_variant.get("title")
    if isinstance(channel_variant.get("delivery"), str) and str(channel_variant.get("delivery")).strip():
        exported["delivery"] = channel_variant.get("delivery")
    if isinstance(channel_variant.get("parse_mode"), str) and str(channel_variant.get("parse_mode")).strip():
        exported["parse_mode"] = channel_variant.get("parse_mode")
    if isinstance(channel_variant.get("binding"), dict):
        exported["binding"] = deepcopy(channel_variant.get("binding"))

    if isinstance(channel_variant.get("actions"), list):
        exported["actions"] = deepcopy(channel_variant.get("actions"))

    if channel == "telegram":
        exported["inline_buttons_enabled"] = channel_variant.get("inline_buttons_enabled", True) is not False
    else:
        exported["template_enabled"] = channel_variant.get("template_enabled", False) is True

    return exported


def export_message_record(message: dict[str, Any], channel: str) -> dict[str, Any]:
    export_variables, rendered_result = build_rendered_result(message, channel)
    normalized = normalize_message_shape(message)
    return {
        "id": normalized.get("id"),
        "name": normalized.get("name"),
        "suite_key": normalized.get("suite_key", ""),
        "group": normalized.get("group", "postoochat"),
        "category": normalized.get("category", ""),
        "channel": channel,
        "variables": export_variables,
        "rendered_result": rendered_result,
        "description": normalized.get("description", ""),
        "assets": hydrate_assets(normalized.get("assets", [])),
        "links": normalize_links(normalized.get("links", [])),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **build_export_channel_fields(normalized, channel),
    }


def build_export_payload(catalog: dict[str, Any]) -> dict[str, Any]:
    messages = [normalize_message_shape(message) for message in catalog.get("messages", []) if isinstance(message, dict)]
    taxonomy = normalize_taxonomy(catalog.get("taxonomy"), messages)
    app_directory = build_suite_app_directory(taxonomy)
    exported_messages: list[dict[str, Any]] = []
    suite_actions = [
        {
            "key": app["app_id"],
            "label": app["label"],
            "triggers": [str(index + 1), app["app_id"]],
            "live": True,
        }
        for index, app in enumerate(app_directory)
    ]
    for message in messages:
        for channel in ("telegram", "whatsapp"):
            exported = export_message_record(message, channel)
            if message.get("group") == "postoochat_suite":
                if message.get("name") == "START_HERE":
                    exported["actions"] = deepcopy(suite_actions)
                if channel == "whatsapp":
                    menu_config = dict(message.get("overrides", {}).get("whatsapp", {}).get("actions_menu", {}))
                    exported["actions_menu"] = menu_config
                    actions = list(exported.get("actions", [])) or deepcopy(message.get("default", {}).get("actions", []))
                    if actions:
                        exported["actions"] = actions
                    if menu_config.get("enabled") is True and actions:
                        instruction = str(menu_config.get("instruction", "Reply with one of the following options."))
                        item_format = str(menu_config.get("item_format", "{index}. {label}"))
                        menu = instruction + "\n\n" + "\n".join(item_format.format(index=index, label=action["label"], key=action["key"]) for index, action in enumerate(actions, start=1))
                        rendered = str(exported.get("rendered_result", "")).rstrip()
                        exported["rendered_result"] = (rendered + "\n\n" + menu).strip()
            exported_messages.append(exported)
    return {
        "source": "message_studio",
        "sent_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "messages": exported_messages,
        "app_directory": app_directory,
    }


def save_export_payload(payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
    EXPORT_PAYLOAD_PATH.write_text(serialized, encoding="utf-8")


def load_saved_export_payload() -> dict[str, Any]:
    if not EXPORT_PAYLOAD_PATH.exists():
        raise FileNotFoundError("Saved export payload not found. Save a message first to generate it.")
    return json.loads(EXPORT_PAYLOAD_PATH.read_text(encoding="utf-8"))


def normalize_collector_group_key(value: Any) -> str:
    return slugify(value)


def read_collectors_file() -> dict[str, Any]:
    if not COLLECTORS_PATH.exists():
        return {}
    try:
        payload = json.loads(COLLECTORS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def sync_collectors(taxonomy: dict[str, list[str]]) -> dict[str, Any]:
    """Load collector scopes from JSON and credentials from the environment.

    Collector configuration is intentionally not regenerated from taxonomy.
    Adding a Message Center group must not silently expand a collector's access.
    """
    del taxonomy  # Scope is explicit in collectors.json, not inferred from catalog data.
    with COLLECTORS_LOCK:
        configured = read_collectors_file()
        synchronized: dict[str, Any] = {}
        for raw_collector_id, raw_config in configured.items():
            collector_id = normalize_collector_group_key(raw_collector_id)
            if not collector_id or not isinstance(raw_config, dict):
                continue

            groups = sorted({
                normalize_collector_group_key(group)
                for group in raw_config.get("groups", [])
                if normalize_collector_group_key(group)
            })
            categories = sorted(
                {str(category or "").strip() for category in raw_config.get("categories", [])},
                key=lambda value: (value != "", value),
            )
            channels = sorted({
                str(channel).strip().lower()
                for channel in raw_config.get("channels", [])
                if str(channel).strip().lower() in {"telegram", "whatsapp"}
            })
            token_env = str(raw_config.get("token_env", "") or "").strip()
            webhook_env = str(raw_config.get("webhook_env", "") or "").strip()
            if not groups or not categories or not channels or not token_env or not webhook_env:
                raise ValueError(
                    f"Collector {collector_id} requires groups, categories, channels, "
                    "token_env, and webhook_env."
                )

            synchronized[collector_id] = {
                "webhook": get_setting(webhook_env).strip(),
                "token": get_setting(token_env).strip(),
                "groups": groups,
                "categories": categories,
                "channels": channels,
            }
        return synchronized


def load_collectors() -> dict[str, Any]:
    catalog = load_catalog()
    return sync_collectors(catalog.get("taxonomy", {}))


def collection_base_url() -> str:
    configured = get_setting("PT_MESSAGE_STUDIO_PUBLIC_URL").strip()
    if configured:
        parsed = urlparse(configured)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return f"http://{HOST}:{PORT}"


def is_collection_authorized(collector: dict[str, Any], authorization_header: str) -> bool:
    expected = str(collector.get("token", "") or "").strip()
    if not expected:
        return False
    scheme, _, supplied = str(authorization_header or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied.strip(), expected)


def notify_export_collectors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    collectors = load_collectors()
    configured = [
        (collector_id, collector)
        for collector_id, collector in collectors.items()
        if str(collector.get("webhook", "") or "").strip()
    ]
    if not configured:
        raise ValueError("No collector webhooks are configured in collectors.json")

    results: list[dict[str, Any]] = []
    for collector_id, collector in configured:
        webhook_url = str(collector["webhook"]).strip()
        parsed = urlparse(webhook_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            results.append({
                "ok": False,
                "collector": collector_id,
                "webhook_url": webhook_url,
                "status": 0,
                "error": "Invalid webhook URL",
            })
            continue
        allowed_messages = filter_export_for_collector(payload, collector, collector_id).get("messages", [])
        notification = {
            "event": "message_export.ready",
            "source": "message_center",
            "collector": collector_id,
            "generated_at": payload.get("sent_at"),
            "message_count": len(allowed_messages),
            "collection_url": f"{collection_base_url()}/api/export/collect/{quote(collector_id)}",
        }
        body = json.dumps(notification, ensure_ascii=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {collector['token']}",
        }
        request = Request(webhook_url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                results.append({
                    "ok": True,
                    "collector": collector_id,
                    "webhook_url": webhook_url,
                    "status": getattr(response, "status", 200),
                })
        except HTTPError as error:
            results.append({
                "ok": False,
                "collector": collector_id,
                "webhook_url": webhook_url,
                "status": error.code,
                "error": error.read().decode("utf-8", errors="replace") or str(error.reason),
            })
        except URLError as error:
            results.append({
                "ok": False,
                "collector": collector_id,
                "webhook_url": webhook_url,
                "status": 0,
                "error": str(error.reason),
            })
    return results


def filter_export_for_collector(
    payload: dict[str, Any],
    collector: dict[str, Any],
    collector_id: str = "collector",
) -> dict[str, Any]:
    groups = {str(value) for value in collector.get("groups", [])}
    categories = {str(value) for value in collector.get("categories", [])}
    channels = {str(value) for value in collector.get("channels", [])}
    filtered = [
        message for message in payload.get("messages", [])
        if isinstance(message, dict)
        and str(message.get("group", "")) in groups
        and str(message.get("category", "")) in categories
        and str(message.get("channel", "")) in channels
    ]
    result = dict(payload)
    result["messages"] = filtered
    result["app_directory"] = [
        app for app in payload.get("app_directory", [])
        if isinstance(app, dict) and str(app.get("message_group", "")) in groups
    ]
    result["collector"] = normalize_collector_group_key(collector_id)
    return result


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PosTooChat Message Center</title>
  <style>
    :root {
      --bg: #f3efe8;
      --panel: #fffdf8;
      --ink: #1c1b18;
      --muted: #6c655c;
      --line: #d8cfc2;
      --accent: #0e766e;
      --accent-soft: #d9f2ef;
      --phone: #111111;
      --bubble: #efe7db;
      --live: #0f766e;
      --idle: #9a3412;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      font-size: 13px;
      line-height: 1.4;
      background:
        radial-gradient(circle at top left, #f9e9d0 0, transparent 28%),
        radial-gradient(circle at bottom right, #d5eee8 0, transparent 32%),
        var(--bg);
      color: var(--ink);
    }
    .app {
      display: grid;
      grid-template-columns: 270px minmax(420px, 1fr) 380px;
      gap: 12px;
      min-height: 100vh;
      padding: 10px;
    }
    .panel {
      min-width: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 12px 28px rgba(40, 33, 22, 0.06);
      overflow: hidden;
    }
    .panel-header {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.75);
    }
    .panel-header h1,
    .panel-header h2 {
      margin: 0;
      font-size: 17px;
      line-height: 1.25;
    }
    .panel-header-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }
    .panel-header p {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .message-list {
      min-width: 0;
      padding: 8px;
      display: grid;
      gap: 8px;
      align-content: start;
    }
    .nav-filters {
      min-width: 0;
      padding: 8px;
      display: grid;
      gap: 10px;
      border-bottom: 1px solid rgba(0,0,0,0.04);
      background: rgba(255,255,255,0.62);
    }
    .nav-filters .field-label {
      min-width: 0;
      gap: 6px;
    }
    .taxonomy-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      margin-top: -2px;
    }
    .taxonomy-actions .btn {
      width: 100%;
      padding: 6px 8px;
      font-size: 10px;
    }
    .nav-filters select {
      display: block;
      width: 100%;
      max-width: 100%;
      min-width: 0;
    }
    .nav-empty {
      border: 1px dashed var(--line);
      border-radius: 12px;
      padding: 12px;
      font-size: 11px;
      line-height: 1.45;
      color: var(--muted);
      background: #faf6ef;
    }
    .nav-group {
      min-width: 0;
      display: grid;
      gap: 8px;
    }
    .nav-group-header {
      min-width: 0;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      padding: 0 2px;
    }
    .nav-group-title {
      min-width: 0;
      overflow-wrap: anywhere;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--ink);
    }
    .nav-group-count,
    .nav-category-count {
      font-size: 10px;
      color: var(--muted);
    }
    .nav-category {
      min-width: 0;
      display: grid;
      gap: 6px;
    }
    .nav-category-header {
      min-width: 0;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 0 2px;
    }
    .nav-category-title {
      min-width: 0;
      overflow-wrap: anywhere;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .nav-category-items {
      min-width: 0;
      display: grid;
      gap: 6px;
    }
    .message-item {
      width: 100%;
      min-width: 0;
      max-width: 100%;
      overflow: hidden;
      border: 1px solid var(--line);
      background: white;
      border-radius: 12px;
      padding: 7px 9px;
      text-align: left;
      cursor: pointer;
      display: grid;
      gap: 4px;
    }
    .message-item.active {
      border-color: var(--accent);
      background: var(--accent-soft);
      box-shadow: inset 3px 0 0 var(--accent);
    }
    .message-item-meta {
      display: block;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: var(--accent);
    }
    .message-item strong {
      display: block;
      min-width: 0;
      font-size: 12px;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }
    .message-item span {
      display: block;
      min-width: 0;
      color: var(--muted);
      font-size: 10.5px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .editor {
      display: grid;
      grid-template-rows: auto auto auto auto auto;
      align-content: start;
      min-height: 0;
      height: auto;
    }
    .toolbar, .variables, .channel-admin {
      padding: 8px 12px;
      border-bottom: 1px solid var(--line);
    }
    .composer {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255,255,255,0.9) 0%, rgba(249,245,238,0.9) 100%);
      display: grid;
      gap: 10px;
    }
    .composer-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .composer-title {
      margin: 0;
      font-size: 13px;
      font-weight: 700;
      color: var(--ink);
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    .composer-copy {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .composer-grid {
      display: grid;
      gap: 10px;
    }
    .composer-section {
      display: grid;
      gap: 8px;
    }
    .composer-section-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .composer-section-title {
      margin: 0;
      font-size: 12px;
      font-weight: 700;
      color: var(--ink);
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }
    .composer-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .composer-parts {
      display: grid;
      gap: 8px;
    }
    .composer-part {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: white;
      padding: 10px;
      display: grid;
      gap: 8px;
    }
    .composer-blocks {
      display: grid;
      gap: 10px;
    }
    .composer-block {
      border: 1px solid #d9cfbf;
      border-radius: 16px;
      background: rgba(255,255,255,0.9);
      padding: 11px;
      display: grid;
      gap: 9px;
    }
    .composer-block-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .composer-block-name {
      font-size: 12px;
      font-weight: 700;
      color: var(--ink);
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }
    .composer textarea {
      height: auto;
      min-height: 88px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      resize: vertical;
    }
    .composer-part-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }
    .composer-part-label {
      font-size: 11px;
      font-weight: 700;
      color: var(--muted);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .composer-format-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .format-chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      background: #f7f1e8;
      color: var(--ink);
      cursor: pointer;
      font-size: 11px;
      font-weight: 600;
    }
    .format-chip.active {
      background: var(--accent-soft);
      border-color: var(--accent);
      color: #0a625c;
    }
    .composer-empty {
      border: 1px dashed var(--line);
      border-radius: 14px;
      padding: 16px;
      color: var(--muted);
      font-size: 12px;
      background: rgba(255,255,255,0.75);
    }
    .toolbar {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .toolbar label, .variables label {
      font-size: 12px;
      color: var(--muted);
      display: grid;
      gap: 6px;
    }
    .variable-card {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px;
      background: white;
      display: grid;
      gap: 8px;
    }
    .variable-name {
      font-size: 12px;
      font-weight: 700;
      color: var(--ink);
      letter-spacing: 0.02em;
    }
    .field-label {
      font-size: 11px;
      color: var(--muted);
      display: grid;
      gap: 6px;
    }
    .channel-admin {
      display: grid;
      gap: 8px;
      background: rgba(255,255,255,0.55);
      min-height: 0;
      overflow: hidden;
    }
    .channel-admin-copy {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .channel-block-list {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      max-height: 260px;
      overflow: auto;
      padding-right: 4px;
    }
    .channel-block-card {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 11px;
      background: white;
      display: grid;
      gap: 6px;
      align-content: start;
    }
    .channel-block-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }
    .channel-block-id {
      font-size: 11px;
      font-weight: 700;
      color: var(--ink);
      letter-spacing: 0.02em;
    }
    .channel-block-preview {
      font-size: 11px;
      color: var(--muted);
      line-height: 1.35;
      white-space: pre-wrap;
    }
    .channel-block-note {
      font-size: 10px;
      color: var(--muted);
    }
    .checkbox-row {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--ink);
      font-size: 11px;
    }
    .checkbox-row input {
      margin: 0;
    }
    select, input, textarea, button {
      font: inherit;
    }
    select, input, textarea {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px 10px;
      background: white;
      color: var(--ink);
    }
    textarea {
      width: 100%;
      height: clamp(280px, 34vh, 460px);
      min-height: 280px;
      display: block;
      resize: vertical;
      overflow: auto;
      border: 0;
      border-radius: 0;
      padding: 14px;
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      line-height: 1.55;
    }
    .variables-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
    }
    .editor-body {
      min-height: 280px;
      border-top: 1px solid rgba(0,0,0,0.02);
      overflow: visible;
    }
    .footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 10px 14px;
      border-top: 1px solid var(--line);
      background: rgba(255,255,255,0.7);
    }
    .footer-actions {
      display:flex;
      gap:10px;
    }
    .btn {
      border: 0;
      border-radius: 999px;
      padding: 8px 14px;
      cursor: pointer;
      background: var(--accent);
      color: white;
      font-size: 12px;
      font-weight: 600;
    }
    .btn.secondary {
      background: #e8e0d3;
      color: var(--ink);
    }
    .btn.danger {
      background: #c95a4a;
      color: white;
    }
    .btn:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }
    .assignment-dialog {
      width: min(460px, calc(100vw - 32px));
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 0;
      color: var(--ink);
      background: var(--panel);
      box-shadow: 0 24px 70px rgba(15, 23, 42, 0.28);
    }
    .assignment-dialog::backdrop {
      background: rgba(15, 23, 42, 0.48);
      backdrop-filter: blur(2px);
    }
    .assignment-dialog form {
      display: grid;
      gap: 18px;
      padding: 22px;
    }
    .assignment-dialog h2,
    .assignment-dialog p {
      margin: 0;
    }
    .assignment-dialog h2 {
      font-size: 17px;
      line-height: 1.25;
    }
    .assignment-dialog p {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .assignment-dialog-fields {
      display: grid;
      gap: 14px;
    }
    .assignment-dialog-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }
    .status {
      font-size: 12px;
      color: var(--muted);
    }
    .preview-wrap {
      padding: 14px;
      display: grid;
      place-items: start center;
      align-self: start;
      position: sticky;
      top: 10px;
      height: calc(100vh - 20px);
      min-height: 0;
      overflow: auto;
      background:
        linear-gradient(180deg, #f9f5ee 0%, #e7efe9 100%);
    }
    .phone {
      width: 330px;
      max-width: 100%;
      min-height: 660px;
      background: var(--phone);
      border-radius: 34px;
      padding: 14px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.25);
    }
    .screen {
      height: 100%;
      min-height: 632px;
      background: #f8f3ec;
      border-radius: 26px;
      overflow: hidden;
      display: grid;
      grid-template-rows: auto 1fr;
    }
    .screen-header {
      padding: 14px 16px;
      background: linear-gradient(180deg, #1c8b81 0%, #0e766e 100%);
      color: white;
    }
    .screen-header strong {
      display: block;
      font-size: 13px;
    }
    .screen-header span {
      font-size: 11px;
      opacity: 0.85;
    }
    .chat {
      padding: 16px;
      display: grid;
      align-content: start;
      gap: 12px;
      overflow: auto;
    }
    .bubble {
      background: var(--bubble);
      border-radius: 18px 18px 18px 6px;
      padding: 14px;
      border: 1px solid #e2d7c8;
      line-height: 1.45;
      font-size: 13px;
    }
    .bubble.is-loading {
      color: var(--muted);
      font-style: italic;
    }
    .bubble.is-error {
      color: #991b1b;
      background: #fef2f2;
      border-color: #fecaca;
    }
    .message-block + .message-block {
      margin-top: 12px;
    }
    .message-title {
      display: block;
      font-weight: 700;
      margin-bottom: 8px;
    }
    .fmt-bold { font-weight: 700; }
    .fmt-italic { font-style: italic; }
    .fmt-underline { text-decoration: underline; }
    .fmt-strike { text-decoration: line-through; }
    .fmt-code {
      font-family: Consolas, "Courier New", monospace;
      background: rgba(0,0,0,0.06);
      border-radius: 6px;
      padding: 1px 5px;
    }
    .fmt-spoiler {
      background: #2d2a26;
      color: #2d2a26;
      border-radius: 4px;
      padding: 0 4px;
    }
    .fmt-spoiler:hover {
      color: #f8f3ec;
    }
    .fmt-quote {
      display: block;
      padding-left: 12px;
      border-left: 3px solid rgba(14, 118, 110, 0.3);
      color: #4f483f;
    }
    .actions {
      display: grid;
      gap: 10px;
    }
    .action {
      border: 1px solid #d6cab9;
      background: white;
      border-radius: 12px;
      padding: 12px;
      text-align: center;
      font-weight: 600;
      font-size: 13px;
    }
    .badge-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .badge {
      font-size: 11px;
      border-radius: 999px;
      padding: 5px 8px;
      background: #eee5d8;
      color: #5e5347;
    }
    .badge.live {
      background: #daf3ef;
      color: var(--live);
    }
    .badge.idle {
      background: #f8e2d7;
      color: var(--idle);
    }
    .formats {
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }
    .asset-gallery {
      margin-top: 14px;
      display: grid;
      gap: 10px;
    }
    .asset-gallery-title {
      font-size: 12px;
      font-weight: 700;
      color: var(--ink);
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }
    .asset-gallery-list {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(104px, 1fr));
      gap: 10px;
    }
    .asset-card {
      overflow: hidden;
      border-radius: 14px;
      background: #f7f1e8;
      border: 1px solid var(--line);
    }
    .asset-card img,
    .asset-card video {
      display: block;
      width: 100%;
      aspect-ratio: 1.15;
      object-fit: cover;
      background: #ece3d5;
    }
    .asset-card audio {
      display: block;
      width: calc(100% - 20px);
      margin: 14px 10px 4px;
    }
    .asset-card-link {
      display: block;
      padding: 18px 12px;
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      text-align: center;
      word-break: break-word;
    }
    .asset-load-error {
      display: none;
      padding: 14px 12px;
      background: #fff1ec;
      color: #a43d22;
      font-size: 12px;
      line-height: 1.4;
    }
    .asset-card.unavailable .asset-load-error {
      display: block;
    }
    .asset-card.unavailable .asset-media {
      display: none;
    }
    .asset-card-copy {
      padding: 8px 10px 10px;
      display: grid;
      gap: 4px;
    }
    .asset-card-type {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: var(--accent);
    }
    .asset-card-path {
      font-size: 11px;
      line-height: 1.35;
      color: var(--muted);
      word-break: break-word;
    }
    .asset-empty {
      border: 1px dashed var(--line);
      border-radius: 14px;
      padding: 12px;
      background: #faf6ef;
      color: var(--muted);
      font-size: 12px;
    }
    .asset-tools {
      margin-top: 14px;
      display: grid;
      gap: 10px;
      padding-top: 14px;
      border-top: 1px solid #e7dccd;
    }
    @media (min-width: 1201px) {
      textarea {
        height: clamp(300px, 36vh, 500px);
      }
    }
    @media (max-width: 1200px) {
      .app {
        grid-template-columns: 1fr;
      }
      .panel.list-panel {
        order: 3;
      }
      .panel.editor {
        order: 2;
      }
      .panel.preview-wrap {
        order: 1;
      }
      .editor {
        height: auto;
        grid-template-rows: auto auto auto auto auto;
        align-content: start;
      }
      .preview-wrap {
        position: static;
        top: auto;
        height: auto;
        min-height: auto;
        overflow: visible;
      }
    }
    @media (max-width: 700px) {
      .app {
        gap: 12px;
        padding: 10px;
      }
      .panel {
        border-radius: 14px;
      }
      .panel-header {
        padding: 12px 14px;
      }
      .toolbar, .variables {
        padding: 12px 14px;
      }
      .message-list {
        grid-template-columns: 1fr;
        padding: 8px;
      }
      .editor {
        grid-template-rows: auto auto auto auto auto;
      }
      .editor-body {
        min-height: 280px;
      }
      .channel-block-list {
        grid-template-columns: 1fr;
        max-height: 220px;
      }
      textarea {
        height: 280px;
        min-height: 280px;
        padding: 14px;
        font-size: 12px;
      }
      .footer {
        flex-direction: column;
        align-items: stretch;
        padding: 12px 14px;
      }
      .footer-actions {
        width: 100%;
      }
      .footer-actions .btn {
        flex: 1;
        min-width: 0;
      }
      .preview-wrap {
        padding: 12px;
      }
      .phone {
        width: 100%;
        min-height: 0;
        border-radius: 24px;
        padding: 10px;
      }
      .screen {
        min-height: 540px;
        border-radius: 18px;
      }
      .chat {
        padding: 12px;
      }
      .bubble {
        padding: 12px;
        font-size: 13px;
      }
      .action {
        padding: 11px;
      }
    }
    @media (max-width: 520px) {
      body {
        font-size: 13px;
      }
      .toolbar {
        display: grid;
        grid-template-columns: 1fr;
      }
      .toolbar label,
      .toolbar button,
      select {
        width: 100%;
      }
      .variables-grid {
        grid-template-columns: 1fr;
      }
      .message-list {
        display: flex;
        overflow-x: auto;
        gap: 8px;
        padding: 8px 8px 10px;
      }
      .message-item {
        min-width: 220px;
      }
      .screen-header {
        padding: 12px 14px;
      }
      .footer-actions {
        flex-direction: column;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="panel list-panel">
      <div class="panel-header">
        <div class="panel-header-row">
          <h1>Message Studio</h1>
          <button class="btn" id="new-message-button" type="button">New Message</button>
        </div>
        <p>Select a group first, then optionally narrow it by category.</p>
      </div>
      <div class="nav-filters">
        <label class="field-label">
          Group
          <select id="group-select"></select>
        </label>
        <div class="taxonomy-actions">
          <button class="btn secondary" id="edit-group-button" type="button">Edit group</button>
          <button class="btn danger" id="remove-group-button" type="button">Remove group</button>
        </div>
        <label class="field-label">
          Category
          <select id="category-select"></select>
        </label>
        <div class="taxonomy-actions">
          <button class="btn secondary" id="edit-category-button" type="button">Edit category</button>
          <button class="btn danger" id="remove-category-button" type="button">Remove category</button>
        </div>
      </div>
      <div class="message-list" id="message-list"></div>
    </aside>

    <section class="panel editor">
      <div class="panel-header">
        <h2 id="editor-title">Message</h2>
        <p id="editor-description">Select a message from the left.</p>
      </div>

      <div class="toolbar">
        <label>
          Channel
          <select id="channel-select">
            <option value="telegram">telegram</option>
            <option value="whatsapp">whatsapp</option>
          </select>
        </label>
        <button class="btn secondary" id="add-paragraph-button" type="button">Add Paragraph</button>
        <button class="btn secondary" id="format-button" type="button">Refresh Preview</button>
      </div>

      <div class="composer">
        <div class="composer-header">
          <div>
            <p class="composer-title">Message Composer</p>
            <p class="composer-copy" id="composer-context">Edit the selected channel's title and body visually. The JSON updates automatically underneath.</p>
          </div>
          <div class="composer-actions">
            <button class="btn secondary" id="add-body-part-button" type="button">Add Text Part</button>
            <button class="btn secondary" id="add-variable-part-button" type="button">Add Variable</button>
            <button class="btn secondary" id="add-block-button" type="button">Add Block</button>
          </div>
        </div>
        <div class="composer-grid">
          <label class="field-label">
            Title
            <input id="composer-title-input" placeholder="Message title">
          </label>
          <div class="composer-section">
            <div class="composer-section-top">
              <p class="composer-section-title">Body Parts</p>
            </div>
            <div class="composer-parts" id="composer-parts"></div>
          </div>
          <div class="composer-section">
            <div class="composer-section-top">
              <p class="composer-section-title">Additional Blocks</p>
            </div>
            <div class="composer-blocks" id="composer-blocks"></div>
          </div>
        </div>
      </div>

      <div class="variables">
        <p style="margin:0 0 10px; color:var(--muted); font-size:11px;">Preview values change the live preview only. Default values update the saved template.</p>
        <div class="variables-grid" id="variables-grid"></div>
      </div>

      <div class="channel-admin">
        <p class="channel-admin-copy">Channel block rules let you make a paragraph optional for the selected channel. Numbered menus auto-hide when inline buttons exist.</p>
        <div class="channel-block-list" id="channel-block-list"></div>
      </div>

      <div class="channel-admin" id="actions-menu-editor" hidden>
        <p class="channel-admin-copy"><strong>WhatsApp action menu</strong> is generated from this message's <code>actions</code>. Its number, label, and trigger all route through the same action key.</p>
        <label class="field-label"><input id="actions-menu-enabled" type="checkbox"> Include the generated menu in this message</label>
        <label class="field-label">Menu instruction<input id="actions-menu-instruction" type="text"></label>
        <label class="field-label">Item format<input id="actions-menu-item-format" type="text" placeholder="{index}. {label}"></label>
      </div>

      <div class="editor-body">
        <textarea id="json-editor" spellcheck="false"></textarea>
      </div>

      <div class="footer">
        <span class="status" id="status-text">Ready.</span>
        <div class="footer-actions">
          <button class="btn secondary" id="post-webhook-button" type="button">Notify</button>
          <button class="btn secondary" id="duplicate-button" type="button">Duplicate</button>
          <button class="btn danger" id="delete-button" type="button">Delete</button>
          <button class="btn secondary" id="reset-button" type="button">Reset</button>
          <button class="btn" id="save-button" type="button">Save</button>
        </div>
      </div>
    </section>

    <section class="panel preview-wrap">
      <div class="phone">
        <div class="screen">
          <div class="screen-header">
            <strong id="phone-channel">telegram</strong>
            <span>preview</span>
          </div>
          <div class="chat">
            <div class="badge-row">
              <span class="badge" id="delivery-badge">loading</span>
              <span class="badge" id="binding-badge">preview</span>
            </div>
            <div class="bubble is-loading" id="bubble" role="status" aria-live="polite">Loading formatted preview…</div>
            <div class="actions" id="actions"></div>
            <div class="formats" id="formats"></div>
            <div class="asset-gallery">
              <div class="asset-gallery-title">Assets</div>
              <div class="asset-gallery-list" id="asset-gallery"></div>
            </div>
            <div class="asset-tools">
              <label class="field-label">
                Media Studio Asset ID
                <input id="asset-key-input" placeholder="example_screenshot">
              </label>
              <label class="field-label">
                Published HTTPS URL
                <input id="asset-url-input" type="url" placeholder="https://media.example.com/assets/example.png">
              </label>
              <label class="field-label">
                Asset Type
                <select id="asset-type-select">
                  <option value="image">image</option>
                  <option value="audio">audio</option>
                  <option value="video">video</option>
                  <option value="document">document</option>
                  <option value="sticker">sticker</option>
                </select>
              </label>
              <button class="btn secondary" id="add-asset-button" type="button">Add Media Reference</button>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>

  <dialog class="assignment-dialog" id="category-assignment-dialog">
    <form id="category-assignment-form">
      <div>
        <h2>Add message to category</h2>
        <p>Choose a destination category and a message from the selected group.</p>
      </div>
      <div class="assignment-dialog-fields">
        <label class="field-label">
          Destination category
          <select id="assignment-category-select"></select>
        </label>
        <label class="field-label">
          Message
          <select id="assignment-message-select"></select>
        </label>
      </div>
      <div class="assignment-dialog-actions">
        <button class="btn secondary" id="assignment-cancel-button" type="button">Cancel</button>
        <button class="btn" id="assignment-submit-button" type="submit">Add message</button>
      </div>
    </form>
  </dialog>

  <script>
    const ADD_GROUP_VALUE = "__add_new_group__";
    const ADD_CATEGORY_VALUE = "__add_new_category__";
    const ADD_MESSAGE_TO_CATEGORY_VALUE = "__add_message_to_category__";
    const ALL_CATEGORIES_VALUE = "__all_categories__";

    const state = {
      catalog: null,
      selectedName: null,
      selectedGroup: "",
      selectedCategory: null,
      dirtyJson: "",
      isDirty: false,
      syncingComposer: false
    };

    const els = {
      list: document.getElementById("message-list"),
      groupSelect: document.getElementById("group-select"),
      categorySelect: document.getElementById("category-select"),
      editGroupButton: document.getElementById("edit-group-button"),
      removeGroupButton: document.getElementById("remove-group-button"),
      editCategoryButton: document.getElementById("edit-category-button"),
      removeCategoryButton: document.getElementById("remove-category-button"),
      assignmentDialog: document.getElementById("category-assignment-dialog"),
      assignmentForm: document.getElementById("category-assignment-form"),
      assignmentCategorySelect: document.getElementById("assignment-category-select"),
      assignmentMessageSelect: document.getElementById("assignment-message-select"),
      assignmentCancelButton: document.getElementById("assignment-cancel-button"),
      assignmentSubmitButton: document.getElementById("assignment-submit-button"),
      editorTitle: document.getElementById("editor-title"),
      editorDescription: document.getElementById("editor-description"),
      channelSelect: document.getElementById("channel-select"),
      composerTitleInput: document.getElementById("composer-title-input"),
      composerContext: document.getElementById("composer-context"),
      composerParts: document.getElementById("composer-parts"),
      composerBlocks: document.getElementById("composer-blocks"),
      addBodyPartButton: document.getElementById("add-body-part-button"),
      addVariablePartButton: document.getElementById("add-variable-part-button"),
      addBlockButton: document.getElementById("add-block-button"),
      jsonEditor: document.getElementById("json-editor"),
      variablesGrid: document.getElementById("variables-grid"),
      channelBlockList: document.getElementById("channel-block-list"),
      actionsMenuEditor: document.getElementById("actions-menu-editor"),
      actionsMenuEnabled: document.getElementById("actions-menu-enabled"),
      actionsMenuInstruction: document.getElementById("actions-menu-instruction"),
      actionsMenuItemFormat: document.getElementById("actions-menu-item-format"),
      statusText: document.getElementById("status-text"),
      phoneChannel: document.getElementById("phone-channel"),
      deliveryBadge: document.getElementById("delivery-badge"),
      bindingBadge: document.getElementById("binding-badge"),
      bubble: document.getElementById("bubble"),
      actions: document.getElementById("actions"),
      formats: document.getElementById("formats"),
      assetGallery: document.getElementById("asset-gallery"),
      assetKeyInput: document.getElementById("asset-key-input"),
      assetUrlInput: document.getElementById("asset-url-input"),
      assetTypeSelect: document.getElementById("asset-type-select"),
      addAssetButton: document.getElementById("add-asset-button"),
      postWebhookButton: document.getElementById("post-webhook-button"),
      duplicateButton: document.getElementById("duplicate-button"),
      deleteButton: document.getElementById("delete-button"),
      saveButton: document.getElementById("save-button"),
      resetButton: document.getElementById("reset-button"),
      formatButton: document.getElementById("format-button"),
      newMessageButton: document.getElementById("new-message-button"),
      addParagraphButton: document.getElementById("add-paragraph-button")
    };

    function slugify(value) {
      return String(value || "")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "") || "message";
    }

    function cloneJson(value) {
      return JSON.parse(JSON.stringify(value));
    }

    function titleizeToken(value) {
      return String(value || "")
        .split(/[_-]+/)
        .filter(Boolean)
        .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
        .join(" ");
    }

    function groupLabel(value) {
      return value === "postoochat" ? "PosToo Suite" : titleizeToken(value) || "Unknown Group";
    }

    function categoryLabel(value) {
      return value ? titleizeToken(value) : "General";
    }

    function normalizeBlockId(messageName, block, index) {
      if (typeof block.id === "string" && block.id.trim()) return block.id.trim();
      if (typeof block.text === "string" && block.text.toLowerCase().includes("reply with any number from numbered-menu")) {
        return "numbered_menu";
      }
      return `${slugify(messageName)}_block_${index + 1}`;
    }

    function normalizeBlocks(messageName, blocks) {
      return (Array.isArray(blocks) ? blocks : []).map((rawBlock, index) => {
        const block = typeof rawBlock === "object" && rawBlock ? { ...rawBlock } : {};
        block.id = normalizeBlockId(messageName, block, index);
        if (block.type === "text" && typeof block.required !== "boolean") {
          block.required = true;
        }
        return block;
      });
    }

    function normalizeBodyBlock(messageName, body) {
      if (!body || typeof body !== "object") return null;
      const normalized = normalizeBlocks(messageName, [{ ...body, id: body.id || "body" }]);
      if (!normalized.length) return null;
      const block = normalized[0];
      if (block.type === "text" && typeof block.required !== "boolean") {
        block.required = true;
      }
      return block;
    }

    function normalizeAssetPath(path) {
      const value = String(path || "")
        .trim()
        .replace(/\\/g, "/")
        .replace(/^\/+|\/+$/g, "");

      if (!value) return "";

      if (
        /^[a-z]+:\/\//i.test(value) ||
        /^localhost/i.test(value) ||
        /^[a-z]:/i.test(value)
      ) {
        return "";
      }

      return value.startsWith("public/")
        ? value.slice("public/".length)
        : value;
    }

    function normalizeAssetKey(value, path, assetType) {
      const raw = String(value || "").trim();
      if (raw) return slugify(raw);
      const normalizedPath = normalizeAssetPath(path || "");
      const filename = normalizedPath.split("/").pop() || "";
      const stem = filename.replace(/\.[^.]+$/, "");
      if (stem) return slugify(stem);
      return slugify(assetType || "asset");
    }

    function normalizeAssetUrl(value) {
      const raw = String(value || "").trim();
      if (!/^https:\/\//i.test(raw)) return "";
      try {
        return new URL(raw).href;
      } catch (_) {
        return "";
      }
    }

    function normalizeAssets(assets) {
      const allowed = new Set(["image", "audio", "video", "document", "sticker"]);
      return (Array.isArray(assets) ? assets : []).map((rawAsset) => {
        if (!rawAsset || typeof rawAsset !== "object") return null;
        const assetType = String(rawAsset.type || "").trim().toLowerCase();
        const assetUrl = normalizeAssetUrl(rawAsset.url);
        if (!allowed.has(assetType) || !assetUrl) return null;
        const normalized = {
          assetId: normalizeAssetKey(rawAsset.assetId || rawAsset.key, "", assetType),
          type: assetType,
          url: assetUrl,
          live: rawAsset.live !== false,
          required: rawAsset.required === true
        };
        if (typeof rawAsset.alt === "string" && rawAsset.alt.trim()) normalized.alt = rawAsset.alt.trim();
        return normalized;
      }).filter(Boolean);
    }

    function normalizeLinks(links) {
      return (Array.isArray(links) ? links : []).map((rawLink) => {
        if (!rawLink || typeof rawLink !== "object") return null;
        const variable = String(rawLink.variable || "").trim();
        if (!variable) return null;
        const normalized = {
          variable,
          live: rawLink.live !== false,
          required: rawLink.required === true,
          mode: "follow_up"
        };
        const key = String(rawLink.key || "").trim();
        if (key) normalized.key = key;
        return normalized;
      }).filter(Boolean);
    }

    function mergeBlocks(messageName, baseBlocks, overrideBlocks) {
      const base = normalizeBlocks(messageName, baseBlocks);
      const override = normalizeBlocks(messageName, overrideBlocks);
      if (!override.length) return base;
      if (!base.length) return override;

      const baseById = new Map(base.map((block) => [block.id, block]));
      const usedIds = new Set();
      const merged = [];

      for (const overrideBlock of override) {
        if (overrideBlock.id && baseById.has(overrideBlock.id)) {
          merged.push({
            ...baseById.get(overrideBlock.id),
            ...overrideBlock
          });
          usedIds.add(overrideBlock.id);
        } else {
          merged.push(overrideBlock);
        }
      }

      for (const baseBlock of base) {
        if (usedIds.has(baseBlock.id)) continue;
        merged.push(baseBlock);
      }
      return merged;
    }

    function normalizeMessage(message) {
      const normalized = cloneJson(message || {});
      const messageName = normalized.name || "message";
      normalized.group = slugify(normalized.group || "postoochat");
      normalized.category = String(normalized.category || "").trim();
      normalized.default = { ...(normalized.default || {}) };
      const defaultBody = normalizeBodyBlock(messageName, normalized.default.body);
      if (defaultBody) {
        normalized.default.body = defaultBody;
      } else {
        delete normalized.default.body;
      }
      normalized.default.blocks = normalizeBlocks(messageName, normalized.default.blocks || []);
      normalized.assets = normalizeAssets(normalized.assets || []);
      normalized.links = normalizeLinks(normalized.links || []);
      normalized.overrides = normalized.overrides || {};
      for (const [channel, rawOverride] of Object.entries(normalized.overrides)) {
        if (!rawOverride || typeof rawOverride !== "object") continue;
        const overrideBody = normalizeBodyBlock(messageName, rawOverride.body);
        normalized.overrides[channel] = {
          ...rawOverride,
          ...(overrideBody ? { body: overrideBody } : {}),
          ...(Array.isArray(rawOverride.blocks) ? { blocks: normalizeBlocks(messageName, rawOverride.blocks) } : {})
        };
      }
      return normalized;
    }

    function parseEditorMessage() {
      const raw = els.jsonEditor.value.trim();
      if (!raw) return null;
      return normalizeMessage(JSON.parse(raw));
    }

    function writeEditorMessage(message) {
      const normalized = normalizeMessage(message);
      els.jsonEditor.value = JSON.stringify(normalized, null, 2);
      updateDirtyState();
      return normalized;
    }

    function restoreSessionMessageView() {
      els.jsonEditor.scrollTop = 0;
      els.jsonEditor.scrollLeft = 0;
    }

    function mergeVariant(base, override) {
      return {
        ...(base || {}),
        ...(override || {}),
        binding: {
          ...((base && base.binding) || {}),
          ...((override && override.binding) || {})
        },
        body: (() => {
          const baseBody = base && base.body && typeof base.body === "object" ? base.body : null;
          const overrideBody = override && override.body && typeof override.body === "object" ? override.body : null;
          if (overrideBody && overrideBody.required === false) return baseBody;
          if (baseBody && overrideBody) return { ...baseBody, ...overrideBody };
          return overrideBody || baseBody;
        })(),
        blocks: mergeBlocks(
          (base && base.name) || (override && override.name) || "message",
          (base && base.blocks) || [],
          (override && override.blocks) || []
        ),
        actions: override && Array.isArray(override.actions) ? override.actions : ((base && base.actions) || [])
      };
    }

    function mergeChannelVariant(message, channel) {
      const variant = mergeVariant(message.default, message.overrides[channel]);
      variant.body = (() => {
        const baseBody = normalizeBodyBlock(message.name || "message", message.default && message.default.body);
        const overrideBody = normalizeBodyBlock(message.name || "message", message.overrides[channel] && message.overrides[channel].body);
        if (overrideBody && overrideBody.required === false) return baseBody;
        if (baseBody && overrideBody) return { ...baseBody, ...overrideBody };
        return overrideBody || baseBody;
      })();
      variant.blocks = mergeBlocks(
        message.name || "message",
        message.default && message.default.blocks,
        message.overrides[channel] && message.overrides[channel].blocks
      );
      return variant;
    }

    function resolvedContentBlocks(variant, messageName) {
      const content = [];
      const usedIds = new Set();
      const bodyBlock = normalizeBodyBlock(messageName, variant && variant.body);
      if (bodyBlock && effectiveBlockRequired(bodyBlock, variant)) {
        content.push(bodyBlock);
        usedIds.add(bodyBlock.id);
      }
      for (const block of normalizeBlocks(messageName, variant && variant.blocks)) {
        if (usedIds.has(block.id)) continue;
        if (!effectiveBlockRequired(block, variant)) continue;
        content.push(block);
      }
      return content;
    }

    function blockPreviewText(block) {
      if (typeof block.text === "string" && block.text.trim()) return block.text.trim();
      if (Array.isArray(block.parts) && block.parts.length) {
        const joined = block.parts
          .map((part) => typeof part.text === "string" ? part.text : "")
          .join("")
          .trim();
        if (joined) return joined;
      }
      return "(formatted block)";
    }

    function ensureBodyBlock(message) {
      const variant = ensureComposerVariant(message);
      if (variant.body && typeof variant.body === "object") {
        variant.body = normalizeBodyBlock(message.name || "message", variant.body);
        return variant.body;
      }
      const body = normalizeBodyBlock(message.name || "message", {
        id: "body",
        type: "text",
        parts: [
          {
            text: "Write",
            format: ["bold", "italic"]
          },
          {
            text: " your message body here."
          }
        ],
        required: true
      });
      variant.body = body;
      return body;
    }

    // The preview has always resolved a channel override, but the composer used
    // the shared default.  Keep the editor and preview on the same variant.
    function ensureComposerVariant(message) {
      const channel = els.channelSelect.value;
      message.overrides = message.overrides || {};
      if (!message.overrides[channel]) {
        message.overrides[channel] = cloneJson(mergeChannelVariant(message, channel));
      }
      return message.overrides[channel];
    }

    function normalizeComposerParts(body) {
      if (!body || typeof body !== "object") return [];
      if (Array.isArray(body.parts) && body.parts.length) {
        return body.parts.map((part) => ({
          text: typeof part.text === "string" ? part.text : "",
          format: Array.isArray(part.format) ? [...part.format] : []
        }));
      }
      if (typeof body.text === "string" && body.text) {
        return [{ text: body.text, format: Array.isArray(body.format) ? [...body.format] : [] }];
      }
      return [];
    }

    function editableDefaultBlocks(message) {
      const variant = mergeChannelVariant(message, els.channelSelect.value);
      const blocks = normalizeBlocks(message.name || "message", variant.blocks);
      return blocks.filter((block) => block.id !== "numbered_menu");
    }

    function normalizeComposableBlock(block) {
      if (!block || typeof block !== "object") return null;
      return {
        ...block,
        parts: normalizeComposerParts(block),
        format: Array.isArray(block.format) ? [...block.format] : []
      };
    }

    function partEditorHtml(part, index, scope, blockIndex = null) {
      const format = Array.isArray(part.format) ? part.format : [];
      const attrs = blockIndex === null
        ? `data-part-scope="${scope}" data-part-index="${index}"`
        : `data-part-scope="${scope}" data-block-index="${blockIndex}" data-part-index="${index}"`;
      const removeAttrs = blockIndex === null
        ? `data-remove-part-scope="${scope}" data-remove-part-index="${index}"`
        : `data-remove-part-scope="${scope}" data-block-index="${blockIndex}" data-remove-part-index="${index}"`;
      const formatAttrs = (name) => blockIndex === null
        ? `data-format-scope="${scope}" data-part-index="${index}" data-format-name="${name}"`
        : `data-format-scope="${scope}" data-block-index="${blockIndex}" data-part-index="${index}" data-format-name="${name}"`;
      return `
        <div class="composer-part">
          <div class="composer-part-top">
            <span class="composer-part-label">Part ${index + 1}</span>
            <button class="btn secondary" type="button" ${removeAttrs}>Remove</button>
          </div>
          <label class="field-label">
            Text
            <textarea ${attrs} rows="3">${escapeHtml(part.text || "")}</textarea>
          </label>
          <div class="composer-format-row">
            ${["bold", "italic", "underline", "strike", "code"].map((name) => `
              <button class="format-chip ${format.includes(name) ? "active" : ""}" type="button" ${formatAttrs(name)}>${name}</button>
            `).join("")}
          </div>
        </div>
      `;
    }

    function renderComposer(message) {
      state.syncingComposer = true;
      const channel = els.channelSelect.value;
      els.composerContext.textContent = `Editing the ${channel} channel content. The JSON updates automatically underneath.`;
      const channelVariant = message ? mergeChannelVariant(message, channel) : {};
      const supportsActionMenu = channel === "whatsapp" && message && message.group === "postoochat_suite";
      els.actionsMenuEditor.hidden = !supportsActionMenu;
      if (supportsActionMenu) {
        const menu = channelVariant.actions_menu || {};
        els.actionsMenuEnabled.checked = menu.enabled === true;
        els.actionsMenuInstruction.value = menu.instruction || "To go to any of the Chat Apps, reply ONLY with the number in front of it.";
        els.actionsMenuItemFormat.value = menu.item_format || "{index}. {label}";
      }
      const body = normalizeBodyBlock(message && message.name || "message", channelVariant.body);
      els.composerTitleInput.value = channelVariant.title || "";
      els.composerParts.innerHTML = "";
      els.composerBlocks.innerHTML = "";

      const parts = normalizeComposerParts(body);
      if (!parts.length) {
        const empty = document.createElement("div");
        empty.className = "composer-empty";
        empty.textContent = "No body block yet. Use Add Text Part to create one.";
        els.composerParts.appendChild(empty);
      } else {
        parts.forEach((part, index) => {
          els.composerParts.insertAdjacentHTML("beforeend", partEditorHtml(part, index, "body"));
        });
      }

      const blocks = editableDefaultBlocks(message || { name: "message", default: {} }).map(normalizeComposableBlock).filter(Boolean);
      if (!blocks.length) {
        const empty = document.createElement("div");
        empty.className = "composer-empty";
        empty.textContent = "No additional blocks yet. Use Add Block to create one.";
        els.composerBlocks.appendChild(empty);
      } else {
        blocks.forEach((block, blockIndex) => {
          const card = document.createElement("div");
          card.className = "composer-block";
          card.innerHTML = `
            <div class="composer-block-header">
              <span class="composer-block-name">${escapeHtml(block.id || `block_${blockIndex + 1}`)}</span>
              <button class="btn secondary" type="button" data-remove-block="${blockIndex}">Remove Block</button>
            </div>
            <div class="composer-format-row">
              ${["bold", "italic", "underline", "strike", "code"].map((name) => `
                <button class="format-chip ${block.format.includes(name) ? "active" : ""}" type="button" data-block-format-index="${blockIndex}" data-block-format-name="${name}">${name}</button>
              `).join("")}
            </div>
            <div class="composer-actions">
              <button class="btn secondary" type="button" data-add-block-text-part="${blockIndex}">Add Text Part</button>
              <button class="btn secondary" type="button" data-add-block-variable-part="${blockIndex}">Add Variable</button>
            </div>
            <div class="composer-parts">
              ${block.parts.map((part, partIndex) => partEditorHtml(part, partIndex, "block", blockIndex)).join("")}
            </div>
          `;
          els.composerBlocks.appendChild(card);
        });
      }

      state.syncingComposer = false;
    }

    function syncComposerIntoEditor(mutator, statusText) {
      if (state.syncingComposer) return;
      try {
        const message = parseEditorMessage();
        if (!message) return;
        mutator(message);
        writeEditorMessage(message);
        renderComposer(message);
        renderVariableInputs(message);
        renderChannelBlockControls(message);
        renderPreview();
        if (statusText) {
          els.statusText.textContent = statusText;
        }
      } catch (error) {
        els.statusText.textContent = "Composer error: " + error.message;
      }
    }

    function updateComposerTitle() {
      syncComposerIntoEditor((message) => {
        ensureComposerVariant(message).title = els.composerTitleInput.value;
      });
    }

    function updateActionsMenu() {
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        variant.actions_menu = {
          ...(variant.actions_menu || {}),
          enabled: els.actionsMenuEnabled.checked,
          source: "actions",
          instruction: els.actionsMenuInstruction.value.trim() || "To go to any of the Chat Apps, reply ONLY with the number in front of it.",
          item_format: els.actionsMenuItemFormat.value.trim() || "{index}. {label}"
        };
      }, els.actionsMenuEnabled.checked ? "Included the generated WhatsApp action menu." : "Removed the generated WhatsApp action menu.");
    }

    function updateComposerPartText(index, value) {
      syncComposerIntoEditor((message) => {
        const body = ensureBodyBlock(message);
        body.parts = normalizeComposerParts(body);
        if (!body.parts[index]) return;
        body.parts[index].text = value;
      });
    }

    function updateBlockComposerPartText(blockIndex, partIndex, value) {
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        const blocks = editableDefaultBlocks(message);
        if (!blocks[blockIndex]) return;
        const normalized = normalizeComposableBlock(blocks[blockIndex]);
        if (!normalized.parts[partIndex]) return;
        normalized.parts[partIndex].text = value;
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []).map((block) => (
          block.id === normalized.id ? { ...normalized } : block
        ));
      });
    }

    function toggleComposerPartFormat(index, formatName) {
      syncComposerIntoEditor((message) => {
        const body = ensureBodyBlock(message);
        body.parts = normalizeComposerParts(body);
        if (!body.parts[index]) return;
        const formats = new Set(Array.isArray(body.parts[index].format) ? body.parts[index].format : []);
        if (formats.has(formatName)) {
          formats.delete(formatName);
        } else {
          formats.add(formatName);
        }
        body.parts[index].format = [...formats];
      }, `Updated ${formatName} formatting.`);
    }

    function toggleBlockComposerPartFormat(blockIndex, partIndex, formatName) {
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        const blocks = editableDefaultBlocks(message);
        if (!blocks[blockIndex]) return;
        const normalized = normalizeComposableBlock(blocks[blockIndex]);
        if (!normalized.parts[partIndex]) return;
        const formats = new Set(Array.isArray(normalized.parts[partIndex].format) ? normalized.parts[partIndex].format : []);
        if (formats.has(formatName)) {
          formats.delete(formatName);
        } else {
          formats.add(formatName);
        }
        normalized.parts[partIndex].format = [...formats];
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []).map((block) => (
          block.id === normalized.id ? { ...normalized } : block
        ));
      }, `Updated ${formatName} formatting.`);
    }

    function toggleBlockComposerFormat(blockIndex, formatName) {
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        const blocks = editableDefaultBlocks(message);
        if (!blocks[blockIndex]) return;
        const normalized = normalizeComposableBlock(blocks[blockIndex]);
        const formats = new Set(Array.isArray(normalized.format) ? normalized.format : []);
        if (formats.has(formatName)) {
          formats.delete(formatName);
        } else {
          formats.add(formatName);
        }
        normalized.format = [...formats];
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []).map((block) => (
          block.id === normalized.id ? { ...normalized } : block
        ));
      }, `Updated block ${formatName} formatting.`);
    }

    function removeComposerPart(index) {
      syncComposerIntoEditor((message) => {
        const body = ensureBodyBlock(message);
        body.parts = normalizeComposerParts(body).filter((_, partIndex) => partIndex !== index);
        if (!body.parts.length) {
          body.parts = [{ text: "", format: [] }];
        }
      }, "Removed body part.");
    }

    function removeBlockComposerPart(blockIndex, partIndex) {
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        const blocks = editableDefaultBlocks(message);
        if (!blocks[blockIndex]) return;
        const normalized = normalizeComposableBlock(blocks[blockIndex]);
        normalized.parts = normalized.parts.filter((_, index) => index !== partIndex);
        if (!normalized.parts.length) {
          normalized.parts = [{ text: "", format: [] }];
        }
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []).map((block) => (
          block.id === normalized.id ? { ...normalized } : block
        ));
      }, "Removed block part.");
    }

    function addComposerTextPart() {
      syncComposerIntoEditor((message) => {
        const body = ensureBodyBlock(message);
        body.parts = normalizeComposerParts(body);
        body.parts.push({ text: "New body text.", format: [] });
      }, "Added body text part.");
    }

    function addBlockComposerTextPart(blockIndex) {
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        const blocks = editableDefaultBlocks(message);
        if (!blocks[blockIndex]) return;
        const normalized = normalizeComposableBlock(blocks[blockIndex]);
        normalized.parts.push({ text: "New block text.", format: [] });
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []).map((block) => (
          block.id === normalized.id ? { ...normalized } : block
        ));
      }, "Added block text part.");
    }

    function addComposerVariablePart() {
      const proposed = window.prompt("Variable name", "new_variable");
      if (!proposed) return;
      const variableName = slugify(proposed);
      if (!variableName) return;
      syncComposerIntoEditor((message) => {
        const body = ensureBodyBlock(message);
        body.parts = normalizeComposerParts(body);
        body.parts.push({ text: `{{${variableName}}}`, format: ["bold"] });
        ensureVariable(message, variableName);
      }, `Added variable ${variableName}.`);
    }

    function ensureVariable(message, variableName) {
      message.variables = message.variables || {};
      if (!message.variables[variableName]) {
        message.variables[variableName] = {
          type: "text",
          required: false,
          default: ""
        };
      }
    }

    function addBlockComposerVariablePart(blockIndex) {
      const proposed = window.prompt("Variable name", "new_variable");
      if (!proposed) return;
      const variableName = slugify(proposed);
      if (!variableName) return;
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        const blocks = editableDefaultBlocks(message);
        if (!blocks[blockIndex]) return;
        const normalized = normalizeComposableBlock(blocks[blockIndex]);
        normalized.parts.push({ text: `{{${variableName}}}`, format: ["bold"] });
        ensureVariable(message, variableName);
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []).map((block) => (
          block.id === normalized.id ? { ...normalized } : block
        ));
      }, `Added variable ${variableName}.`);
    }

    function addComposerBlock() {
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []);
        const newBlock = {
          id: nextParagraphId(message),
          type: "text",
          parts: [
            {
              text: "New",
              format: ["bold"]
            },
            {
              text: " block text."
            }
          ],
          format: [],
          required: true
        };
        const numberedMenuIndex = variant.blocks.findIndex((block) => block.id === "numbered_menu");
        if (numberedMenuIndex >= 0) {
          variant.blocks.splice(numberedMenuIndex, 0, newBlock);
        } else {
          variant.blocks.push(newBlock);
        }
      }, "Added a new block.");
    }

    function removeComposerBlock(blockIndex) {
      syncComposerIntoEditor((message) => {
        const variant = ensureComposerVariant(message);
        const editableBlocks = editableDefaultBlocks(message);
        if (!editableBlocks[blockIndex]) return;
        const blockId = editableBlocks[blockIndex].id;
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []).filter((block) => block.id !== blockId);
      }, "Removed block.");
    }

    function channelUsesInlineActions(variant) {
      return (variant.actions || []).length > 0 && ["inline_buttons", "interactive"].includes(variant.delivery || "");
    }

    function applyChannelBehavior(variant, channel) {
      const effective = {
        ...variant,
        binding: { ...(variant.binding || {}) },
        actions: Array.isArray(variant.actions) ? [...variant.actions] : [],
        blocks: Array.isArray(variant.blocks) ? [...variant.blocks] : []
      };

      if (channel === "telegram") {
        const inlineButtonsEnabled = effective.inline_buttons_enabled !== false;
        if (!inlineButtonsEnabled) {
          effective.delivery = "plain_text";
          effective.actions = [];
        }
        return effective;
      }

      const templateEnabled = effective.template_enabled === true;
      if (!templateEnabled) {
        effective.delivery = "plain_text";
        effective.actions = [];
      }
      return effective;
    }

    function effectiveBlockRequired(block, variant) {
      const explicitRequired = typeof block.required === "boolean" ? block.required : true;
      if (!explicitRequired) return false;
      if (block.id === "numbered_menu" && channelUsesInlineActions(variant)) return false;
      return true;
    }

    function buildNumberedMenuText() {
      return "Reply with any number from numbered-menu.\\n\\n1. Previous Message\\n2. Next Message";
    }

    function nextParagraphId(message) {
      const blocks = normalizeBlocks(message.name || "message", message.default && message.default.blocks);
      const usedIds = new Set(blocks.map((block) => block.id));
      let index = blocks.length + 1;
      let candidate = `paragraph_${index}`;
      while (usedIds.has(candidate)) {
        index += 1;
        candidate = `paragraph_${index}`;
      }
      return candidate;
    }

    async function loadCatalog() {
      try {
        const response = await fetch("/api/catalog");
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || `Catalog request failed (${response.status})`);
        }
        state.catalog = {
          taxonomy: payload.taxonomy && typeof payload.taxonomy === "object" ? payload.taxonomy : {},
          messages: Array.isArray(payload.messages) ? payload.messages : []
        };
        const initialMessages = state.catalog.messages
          .map(normalizeMessage)
          .sort((left, right) => String(left.name || "").localeCompare(String(right.name || "")));
        if (!state.selectedGroup && initialMessages.length) {
          state.selectedGroup = initialMessages[0].group;
        }
        renderNavFilters();
        renderMessageList();
        if (initialMessages.length) {
          const initialMessage = initialMessages.find((message) => message.group === state.selectedGroup);
          if (initialMessage) {
            selectMessage(initialMessage.name);
          }
        } else {
          els.statusText.textContent = "No messages found in messages.json.";
        }
      } catch (error) {
        state.catalog = { taxonomy: {}, messages: [] };
        renderNavFilters();
        renderMessageList(`Could not load messages. Start the studio server and refresh. (${error.message})`);
        els.statusText.textContent = "Catalog load error: " + error.message;
        paintPreviewError(error);
      }
    }

    function renderNavFilters() {
      const messages = state.catalog && Array.isArray(state.catalog.messages) ? state.catalog.messages.map(normalizeMessage) : [];
      const groupKeys = [...new Set([
        ...Object.keys((state.catalog && state.catalog.taxonomy) || {}),
        ...messages.map((message) => message.group).filter(Boolean)
      ])].sort((left, right) => left.localeCompare(right));
      if (state.selectedGroup && !groupKeys.includes(state.selectedGroup)) {
        state.selectedGroup = "";
      }

      const groupOptions = ['<option value="">Select group</option>']
        .concat(groupKeys.map((group) => `<option value="${escapeHtml(group)}"${group === state.selectedGroup ? " selected" : ""}>${escapeHtml(groupLabel(group))}</option>`))
        .concat([`<option value="${ADD_GROUP_VALUE}">Add a new group.</option>`]);
      els.groupSelect.innerHTML = groupOptions.join("");

      const categoryKeys = state.selectedGroup
        ? [...new Set([
            ...((((state.catalog || {}).taxonomy || {})[state.selectedGroup]) || []),
            ...messages
              .filter((message) => message.group === state.selectedGroup)
              .map((message) => message.category)
          ])]
            // Every group has an internal blank category. Only show it when it
            // actually contains messages; otherwise it would duplicate "General".
            .filter((category) => category !== "" || messages.some((message) =>
              message.group === state.selectedGroup && message.category === ""
            ))
            .sort((left, right) => String(left).localeCompare(String(right)))
        : [];

      if (state.selectedCategory !== null && !categoryKeys.includes(state.selectedCategory)) {
        state.selectedCategory = null;
      }

      const categoryOptions = [`<option value="${ALL_CATEGORIES_VALUE}"${state.selectedCategory === null ? " selected" : ""}>All categories</option>`]
        .concat(categoryKeys.map((category) => `<option value="${escapeHtml(category)}"${category === state.selectedCategory ? " selected" : ""}>${escapeHtml(categoryLabel(category))}</option>`));
      if (state.selectedGroup) {
        categoryOptions.push(`<option value="${ADD_CATEGORY_VALUE}">Add a new category.</option>`);
      }
      if (state.selectedGroup) {
        categoryOptions.push(`<option value="${ADD_MESSAGE_TO_CATEGORY_VALUE}">Add message to category.</option>`);
      }
      els.categorySelect.innerHTML = categoryOptions.join("");
      els.categorySelect.disabled = !state.selectedGroup;
      els.editGroupButton.disabled = !state.selectedGroup;
      els.removeGroupButton.disabled = !state.selectedGroup || groupKeys.length <= 1;
      els.editCategoryButton.disabled = state.selectedCategory === null || state.selectedCategory === "";
      els.removeCategoryButton.disabled = state.selectedCategory === null || state.selectedCategory === "";
    }

    function renderMessageList(emptyMessage = "No messages found.") {
      els.list.innerHTML = "";
      const messages = state.catalog && Array.isArray(state.catalog.messages) ? state.catalog.messages : [];
      if (!messages.length) {
        const empty = document.createElement("div");
        empty.className = "composer-empty";
        empty.textContent = emptyMessage;
        els.list.appendChild(empty);
        return;
      }

      if (!state.selectedGroup) {
        const empty = document.createElement("div");
        empty.className = "nav-empty";
        empty.textContent = "Choose a group from the dropdown above to show its messages.";
        els.list.appendChild(empty);
        return;
      }

      const grouped = new Map();
      const sortedMessages = [...messages]
        .map(normalizeMessage)
        .filter((message) => message.group === state.selectedGroup)
        .filter((message) => state.selectedCategory === null || message.category === state.selectedCategory)
        .sort((left, right) => String(left.name || "").localeCompare(String(right.name || "")));

      if (!sortedMessages.length) {
        const empty = document.createElement("div");
        empty.className = "nav-empty";
        empty.textContent = state.selectedCategory !== null
          ? "No messages match this group and category yet."
          : "No messages exist in this group yet.";
        els.list.appendChild(empty);
        return;
      }

      for (const message of sortedMessages) {
        const groupKey = message.group;
        const categoryKey = message.category || "__general__";
        if (!grouped.has(groupKey)) {
          grouped.set(groupKey, new Map());
        }
        const categories = grouped.get(groupKey);
        if (!categories.has(categoryKey)) {
          categories.set(categoryKey, []);
        }
        categories.get(categoryKey).push(message);
      }

      for (const [groupKey, categories] of grouped.entries()) {
        const groupSection = document.createElement("section");
        groupSection.className = "nav-group";
        const groupCount = [...categories.values()].reduce((count, categoryMessages) => count + categoryMessages.length, 0);
        groupSection.innerHTML = `
          <div class="nav-group-header">
            <span class="nav-group-title">${escapeHtml(groupLabel(groupKey) || "Messages")}</span>
            <span class="nav-group-count">${groupCount}</span>
          </div>
        `;

        for (const [categoryKey, categoryMessages] of categories.entries()) {
          const categorySection = document.createElement("div");
          categorySection.className = "nav-category";
          const currentCategoryLabel = categoryKey === "__general__" ? "General" : categoryLabel(categoryKey);
          categorySection.innerHTML = `
            <div class="nav-category-header">
              <span class="nav-category-title">${escapeHtml(currentCategoryLabel)}</span>
              <span class="nav-category-count">${categoryMessages.length}</span>
            </div>
          `;
          const items = document.createElement("div");
          items.className = "nav-category-items";
          for (const message of categoryMessages) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "message-item" + (message.name === state.selectedName ? " active" : "");
            const description = message.description || "No description yet.";
            const meta = message.category ? currentCategoryLabel : "General";
            button.innerHTML = `<span class="message-item-meta">${escapeHtml(meta)}</span><strong>${escapeHtml(message.name)}</strong><span>${escapeHtml(description)}</span>`;
            button.addEventListener("click", () => selectMessage(message.name));
            items.appendChild(button);
          }
          categorySection.appendChild(items);
          groupSection.appendChild(categorySection);
        }
        els.list.appendChild(groupSection);
      }
    }

    function getSelectedMessage() {
      return state.catalog && Array.isArray(state.catalog.messages)
        ? state.catalog.messages.find((message) => message.name === state.selectedName) || null
        : null;
    }

    function getSavedSelectedJson() {
      const message = getSelectedMessage();
      if (!message) return "";
      return JSON.stringify(normalizeMessage(message), null, 2);
    }

    function updateActionAvailability() {
      els.postWebhookButton.disabled = state.isDirty || !state.selectedName;
      els.duplicateButton.disabled = !state.selectedName;
      els.deleteButton.disabled = !state.selectedName;
    }

    function updateDirtyState() {
      const savedJson = getSavedSelectedJson();
      state.isDirty = Boolean(state.selectedName) && els.jsonEditor.value !== savedJson;
      updateActionAvailability();
    }

    function nextMessageId() {
      const ids = state.catalog.messages
        .map((message) => Number(message.id || 0))
        .filter((value) => Number.isFinite(value));
      return ids.length ? Math.max(...ids) + 1 : 1001;
    }

    function buildNewMessageName() {
      const base = "NEW_MESSAGE";
      const existing = new Set(state.catalog.messages.map((message) => message.name));
      if (!existing.has(base)) return base;
      let index = 2;
      while (existing.has(`${base}_${index}`)) index += 1;
      return `${base}_${index}`;
    }

    function buildDuplicateMessageName(sourceName) {
      const base = `${String(sourceName || "MESSAGE")}_COPY`;
      const existing = new Set(state.catalog.messages.map((message) => message.name));
      if (!existing.has(base)) return base;
      let index = 2;
      while (existing.has(`${base}_${index}`)) index += 1;
      return `${base}_${index}`;
    }

    function buildBlankMessage() {
      return normalizeMessage({
        name: buildNewMessageName(),
        id: nextMessageId(),
        group: state.selectedGroup || "postoochat",
        category: state.selectedCategory ?? "",
        description: "Describe what this message is for.",
        variables: {},
        links: [],
        default: {
          title: "New Message",
          body: {
            id: "body",
            type: "text",
            parts: [
              {
                text: "Write",
                format: ["bold", "italic"]
              },
              {
                text: " your new message body here."
              }
            ],
            required: true
          },
          blocks: [
            {
              id: "numbered_menu",
              type: "text",
              text: buildNumberedMenuText(),
              required: true
            }
          ],
          actions: [
            {
              key: "previous_message",
              label: "Previous Message",
              action: "menu:previous-message",
              live: true
            },
            {
              key: "next_message",
              label: "Next Message",
              action: "menu:next-message",
              live: true
            }
          ]
        },
        overrides: {
          telegram: {
            delivery: "inline_buttons",
            parse_mode: "Markdown",
            inline_buttons_enabled: true
          },
          whatsapp: {
            delivery: "interactive",
            template_enabled: false,
            binding: {
              content_sid_env: ""
            }
          }
        }
      });
    }

    function selectMessage(name) {
      state.selectedName = name;
      renderMessageList();
      const message = getSelectedMessage();
      if (!message) {
        clearSelection();
        return;
      }
      els.editorTitle.textContent = message.name;
      els.editorDescription.textContent = message.description || "No description yet.";
      const normalized = normalizeMessage(message);
      state.dirtyJson = JSON.stringify(normalized, null, 2);
      els.jsonEditor.value = state.dirtyJson;
      updateDirtyState();
      restoreSessionMessageView();
      renderComposer(normalized);
      renderVariableInputs(normalized);
      renderChannelBlockControls(normalized);
      renderPreview();
    }

    function clearSelection() {
      state.selectedName = null;
      state.dirtyJson = "";
      els.editorTitle.textContent = "Message";
      els.editorDescription.textContent = "Select a message from the left.";
      els.composerTitleInput.value = "";
      els.composerParts.innerHTML = '<div class="composer-empty">Select a message to start composing.</div>';
      els.composerBlocks.innerHTML = '<div class="composer-empty">Select a message to manage extra blocks.</div>';
      els.jsonEditor.value = "";
      els.variablesGrid.innerHTML = "";
      els.channelBlockList.innerHTML = '<div class="channel-block-note">Select a message to manage channel block rules.</div>';
      els.bubble.className = "bubble";
      els.bubble.textContent = "Select a message to preview.";
      els.actions.innerHTML = "";
      els.formats.textContent = "";
      els.deliveryBadge.textContent = "delivery";
      els.bindingBadge.textContent = "binding";
      updateDirtyState();
      renderMessageList();
    }

    async function updateTaxonomy(action, details = {}) {
      const response = await fetch("/api/taxonomy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, ...details })
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Folder update failed");
      }
      await loadCatalog();
      return payload;
    }

    async function handleGroupChange() {
      const selectedValue = els.groupSelect.value;
      if (selectedValue === ADD_GROUP_VALUE) {
        const enteredName = window.prompt("Enter the new group name:");
        const group = slugify(enteredName);
        if (!enteredName || !enteredName.trim()) {
          renderNavFilters();
          return;
        }
        const existingGroups = new Set(state.catalog.messages.map((message) => normalizeMessage(message).group));
        if (existingGroups.has(group)) {
          state.selectedGroup = group;
          state.selectedCategory = null;
          renderNavFilters();
          renderMessageList();
          els.statusText.textContent = `${groupLabel(group)} already exists.`;
          return;
        }
        state.selectedGroup = group;
        state.selectedCategory = null;
        try {
          await updateTaxonomy("create_group", { group });
          els.statusText.textContent = `Created ${groupLabel(group)}.`;
        } catch (error) {
          els.statusText.textContent = "Group creation error: " + error.message;
        }
        return;
      }

      state.selectedGroup = selectedValue;
      state.selectedCategory = null;
      const selected = getSelectedMessage();
      if (selected) {
        const normalized = normalizeMessage(selected);
        if (normalized.group !== state.selectedGroup) {
          clearSelection();
          renderNavFilters();
          return;
        }
      }
      renderNavFilters();
      renderMessageList();
    }

    async function handleCategoryChange() {
      const selectedValue = els.categorySelect.value;
      if (selectedValue === ADD_MESSAGE_TO_CATEGORY_VALUE) {
        await addMessageToSelectedCategory();
        renderNavFilters();
        return;
      }

      if (selectedValue === ADD_CATEGORY_VALUE) {
        const enteredName = window.prompt("Enter the new category name:");
        const category = slugify(enteredName);
        if (!enteredName || !enteredName.trim()) {
          renderNavFilters();
          return;
        }
        const existingCategories = new Set(
          state.catalog.messages
            .map(normalizeMessage)
            .filter((message) => message.group === state.selectedGroup)
            .map((message) => message.category)
        );
        if (existingCategories.has(category)) {
          state.selectedCategory = category;
          renderNavFilters();
          renderMessageList();
          els.statusText.textContent = `${categoryLabel(category)} already exists in this group.`;
          return;
        }
        state.selectedCategory = category;
        try {
          await updateTaxonomy("create_category", { group: state.selectedGroup, new_key: category });
          els.statusText.textContent = `Created ${categoryLabel(category)}.`;
        } catch (error) {
          els.statusText.textContent = "Category creation error: " + error.message;
        }
        return;
      }

      state.selectedCategory = selectedValue === ALL_CATEGORIES_VALUE ? null : selectedValue;
      const selected = getSelectedMessage();
      if (selected) {
        const normalized = normalizeMessage(selected);
        const matches = normalized.group === state.selectedGroup &&
          (state.selectedCategory === null || normalized.category === state.selectedCategory);
        if (!matches) {
          clearSelection();
          renderNavFilters();
          return;
        }
      }
      renderMessageList();
    }

    async function editSelectedGroup() {
      if (!state.selectedGroup) return;
      const currentGroup = state.selectedGroup;
      const enteredName = window.prompt("Rename group:", groupLabel(currentGroup));
      if (!enteredName || !enteredName.trim()) return;
      const newGroup = slugify(enteredName);
      if (newGroup === currentGroup) return;
      try {
        state.selectedGroup = newGroup;
        state.selectedCategory = null;
        await updateTaxonomy("rename_group", { group: currentGroup, new_key: newGroup });
        els.statusText.textContent = `Renamed ${groupLabel(currentGroup)} to ${groupLabel(newGroup)}.`;
      } catch (error) {
        state.selectedGroup = currentGroup;
        els.statusText.textContent = "Group rename error: " + error.message;
        renderNavFilters();
      }
    }

    async function removeSelectedGroup() {
      if (!state.selectedGroup) return;
      const group = state.selectedGroup;
      const remainingGroups = Object.keys(state.catalog.taxonomy || {}).filter((key) => key !== group);
      if (!remainingGroups.length) return;
      const fallbackGroup = remainingGroups[0];
      const affectedCount = state.catalog.messages.filter((message) => normalizeMessage(message).group === group).length;
      const confirmed = window.confirm(
        `Remove ${groupLabel(group)}? ${affectedCount} message(s) will move to ${groupLabel(fallbackGroup)}.`
      );
      if (!confirmed) return;
      try {
        state.selectedGroup = fallbackGroup;
        state.selectedCategory = null;
        await updateTaxonomy("remove_group", { group, fallback_group: fallbackGroup });
        els.statusText.textContent = `Removed ${groupLabel(group)}. Its messages were moved to ${groupLabel(fallbackGroup)}.`;
      } catch (error) {
        state.selectedGroup = group;
        els.statusText.textContent = "Group removal error: " + error.message;
        renderNavFilters();
      }
    }

    async function editSelectedCategory() {
      if (!state.selectedGroup || !state.selectedCategory) return;
      const currentCategory = state.selectedCategory;
      const enteredName = window.prompt("Rename category:", categoryLabel(currentCategory));
      if (!enteredName || !enteredName.trim()) return;
      const newCategory = slugify(enteredName);
      if (newCategory === currentCategory) return;
      try {
        state.selectedCategory = newCategory;
        await updateTaxonomy("rename_category", {
          group: state.selectedGroup,
          category: currentCategory,
          new_key: newCategory
        });
        els.statusText.textContent = `Renamed ${categoryLabel(currentCategory)} to ${categoryLabel(newCategory)}.`;
      } catch (error) {
        state.selectedCategory = currentCategory;
        els.statusText.textContent = "Category rename error: " + error.message;
        renderNavFilters();
      }
    }

    async function removeSelectedCategory() {
      if (!state.selectedGroup || !state.selectedCategory) return;
      const category = state.selectedCategory;
      const affectedCount = state.catalog.messages.filter((message) => {
        const normalized = normalizeMessage(message);
        return normalized.group === state.selectedGroup && normalized.category === category;
      }).length;
      const confirmed = window.confirm(
        `Remove ${categoryLabel(category)}? ${affectedCount} message(s) will move to General.`
      );
      if (!confirmed) return;
      try {
        state.selectedCategory = "";
        await updateTaxonomy("remove_category", { group: state.selectedGroup, category });
        els.statusText.textContent = `Removed ${categoryLabel(category)}. Its messages were moved to General.`;
      } catch (error) {
        state.selectedCategory = category;
        els.statusText.textContent = "Category removal error: " + error.message;
        renderNavFilters();
      }
    }

    function chooseCategoryAssignment() {
      const groupMessages = state.catalog.messages
        .map(normalizeMessage)
        .filter((message) => message.group === state.selectedGroup);
      const categories = [...new Set(
        [
          ...(((state.catalog.taxonomy || {})[state.selectedGroup]) || []),
          ...groupMessages.map((message) => message.category)
        ]
      )].sort((left, right) => categoryLabel(left).localeCompare(categoryLabel(right)));

      if (!categories.length) {
        els.statusText.textContent = "Create a category in this group before assigning messages to it.";
        return Promise.resolve(null);
      }

      els.assignmentCategorySelect.innerHTML = categories.map((category) =>
        `<option value="${escapeHtml(category)}">${escapeHtml(categoryLabel(category))}</option>`
      ).join("");
      const preferredCategory = categories.find((category) =>
        category !== state.selectedCategory &&
        groupMessages.some((message) => message.category !== category)
      );
      els.assignmentCategorySelect.value = preferredCategory ?? categories[0];

      const updateMessageOptions = () => {
        const targetCategory = els.assignmentCategorySelect.value;
        const candidates = groupMessages
          .filter((message) => message.category !== targetCategory)
          .sort((left, right) => String(left.name || "").localeCompare(String(right.name || "")));
        els.assignmentMessageSelect.innerHTML = candidates.map((message) => {
          const currentCategory = message.category ? categoryLabel(message.category) : "General";
          return `<option value="${escapeHtml(message.name)}">${escapeHtml(message.name)} — ${escapeHtml(currentCategory)}</option>`;
        }).join("");
        els.assignmentMessageSelect.disabled = !candidates.length;
        els.assignmentSubmitButton.disabled = !candidates.length;
      };
      updateMessageOptions();

      return new Promise((resolve) => {
        const cleanup = () => {
          els.assignmentCategorySelect.removeEventListener("change", updateMessageOptions);
          els.assignmentForm.removeEventListener("submit", handleSubmit);
          els.assignmentCancelButton.removeEventListener("click", handleCancel);
          els.assignmentDialog.removeEventListener("cancel", handleCancel);
        };
        const finish = (result) => {
          cleanup();
          if (els.assignmentDialog.open) els.assignmentDialog.close();
          resolve(result);
        };
        const handleSubmit = (event) => {
          event.preventDefault();
          const targetCategory = els.assignmentCategorySelect.value;
          const messageName = els.assignmentMessageSelect.value;
          const message = groupMessages.find((candidate) => candidate.name === messageName);
          finish(message ? { message, targetCategory } : null);
        };
        const handleCancel = (event) => {
          event.preventDefault();
          finish(null);
        };

        els.assignmentCategorySelect.addEventListener("change", updateMessageOptions);
        els.assignmentForm.addEventListener("submit", handleSubmit);
        els.assignmentCancelButton.addEventListener("click", handleCancel);
        els.assignmentDialog.addEventListener("cancel", handleCancel);
        els.assignmentDialog.showModal();
      });
    }

    async function addMessageToSelectedCategory() {
      if (!state.selectedGroup) return;
      const assignment = await chooseCategoryAssignment();
      if (!assignment) return;

      const { message, targetCategory } = assignment;
      const previousCategory = message.category ? categoryLabel(message.category) : "General";
      message.category = targetCategory;
      state.selectedCategory = targetCategory;

      try {
        const response = await fetch("/api/messages/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            selected_name: message.name,
            message
          })
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Category assignment failed");
        }
        await loadCatalog();
        selectMessage(payload.message.name);
        els.statusText.textContent =
          `Moved ${payload.message.name} from ${previousCategory} to ${categoryLabel(targetCategory)}.`;
      } catch (error) {
        els.statusText.textContent = "Category assignment error: " + error.message;
      }
    }

    async function createNewMessage() {
      try {
        const message = buildBlankMessage();
        const response = await fetch("/api/messages/create", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message })
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Create failed");
        }
        await loadCatalog();
        selectMessage(payload.message.name);
        els.statusText.textContent = `Created ${payload.message.name}.`;
        return payload.message;
      } catch (error) {
        els.statusText.textContent = "Create error: " + error.message;
        return null;
      }
    }

    async function duplicateSelectedMessage() {
      try {
        const selected = getSelectedMessage();
        if (!selected) return;
        const message = normalizeMessage(cloneJson(selected));
        message.name = buildDuplicateMessageName(selected.name);
        message.id = nextMessageId();
        const response = await fetch("/api/messages/create", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message })
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Duplicate failed");
        }
        await loadCatalog();
        selectMessage(payload.message.name);
        els.statusText.textContent = `Duplicated ${selected.name} to ${payload.message.name}.`;
      } catch (error) {
        els.statusText.textContent = "Duplicate error: " + error.message;
      }
    }

    async function deleteSelectedMessage() {
      try {
        const message = getSelectedMessage();
        if (!message || !state.selectedName) return;
        const confirmed = window.confirm(`Delete ${message.name}? This removes it from messages.json.`);
        if (!confirmed) return;

        const response = await fetch("/api/messages/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ selected_name: state.selectedName })
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Delete failed");
        }

        const fallbackName = payload.next_selected_name || null;
        await loadCatalog();
        if (fallbackName) {
          selectMessage(fallbackName);
        } else {
          clearSelection();
        }
        els.statusText.textContent = `Deleted ${payload.deleted_name}.`;
      } catch (error) {
        els.statusText.textContent = "Delete error: " + error.message;
      }
    }

    function renderVariableInputs(message) {
      els.variablesGrid.innerHTML = "";
      const variables = message.variables || {};
      const entries = Object.entries(variables);
      if (!entries.length) {
        const empty = document.createElement("div");
        empty.style.color = "var(--muted)";
        empty.style.fontSize = "12px";
        empty.textContent = "No variables for this message yet.";
        els.variablesGrid.appendChild(empty);
        return;
      }
      for (const [name, config] of entries) {
        const wrap = document.createElement("div");
        wrap.className = "variable-card";
        wrap.innerHTML = `
          <div class="variable-name">${name}</div>
          <label class="field-label">
            Preview value
            <input data-variable="${name}" placeholder="${config.default || ""}" value="">
          </label>
          <label class="field-label">
            Default value
            <input data-default-variable="${name}" value="${escapeAttribute(config.default || "")}">
          </label>
        `;
        wrap.querySelector("[data-variable]").addEventListener("input", renderPreview);
        wrap.querySelector("[data-default-variable]").addEventListener("input", syncVariableDefaultsIntoEditor);
        els.variablesGrid.appendChild(wrap);
      }
    }

    function renderChannelBlockControls(message) {
      els.channelBlockList.innerHTML = "";
      const variant = applyChannelBehavior(mergeChannelVariant(message, els.channelSelect.value), els.channelSelect.value);
      const blocks = resolvedContentBlocks(variant, message.name || "message").filter((block) => block.type === "text");
      if (!blocks.length) {
        const empty = document.createElement("div");
        empty.className = "channel-block-note";
        empty.textContent = "No text blocks available for this channel.";
        els.channelBlockList.appendChild(empty);
        return;
      }
      for (const block of blocks) {
        const card = document.createElement("div");
        card.className = "channel-block-card";
        const autoOptional = block.id === "numbered_menu" && channelUsesInlineActions(variant);
        const lockedRequired = block.id === "body";
        const checked = effectiveBlockRequired(block, variant);
        card.innerHTML = `
          <div class="channel-block-top">
            <div class="channel-block-id">${escapeHtml(block.id || "block")}</div>
          </div>
          <div class="channel-block-preview">${escapeHtml(blockPreviewText(block))}</div>
          <label class="checkbox-row">
            <input type="checkbox" data-block-required="${escapeAttribute(block.id || "")}" ${checked ? "checked" : ""} ${(autoOptional || lockedRequired) ? "disabled" : ""}>
            <span>Required for ${escapeHtml(els.channelSelect.value)}</span>
          </label>
          <div class="channel-block-note">${lockedRequired ? "The body block is the primary message content and always stays visible." : (autoOptional ? "Inline actions are present, so the numbered menu is auto-hidden for this channel." : "This setting is saved into the selected channel override.")}</div>
        `;
        const checkbox = card.querySelector("[data-block-required]");
        if (checkbox) {
          checkbox.addEventListener("change", (event) => {
            setBlockRequiredForChannel(block.id, event.target.checked);
          });
        }
        els.channelBlockList.appendChild(card);
      }
    }

    function escapeAttribute(value) {
      return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll('"', "&quot;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function syncVariableDefaultsIntoEditor() {
      const raw = els.jsonEditor.value.trim();
      if (!raw) return;
      try {
        const message = parseEditorMessage();
        if (!message) return;
        const variables = message.variables || {};
        for (const [name] of Object.entries(variables)) {
          const input = els.variablesGrid.querySelector(`[data-default-variable="${name}"]`);
          if (!input) continue;
          if (!message.variables[name]) continue;
          message.variables[name].default = input.value;
        }
        writeEditorMessage(message);
        renderChannelBlockControls(message);
        renderPreview();
        els.statusText.textContent = "Default values synced into JSON.";
      } catch (error) {
        els.statusText.textContent = "Default sync error: " + error.message;
      }
    }

    function currentVariablesFromForm(message) {
      const variables = {};
      const configs = message.variables || {};
      for (const [name, config] of Object.entries(configs)) {
        const input = els.variablesGrid.querySelector(`[data-variable="${name}"]`);
        const value = input ? input.value : "";
        variables[name] = value || config.default || "";
      }
      return variables;
    }

    async function renderPreview() {
      const raw = els.jsonEditor.value.trim();
      if (!raw) return;
      try {
        const message = parseEditorMessage();
        if (!message) return;
        const defaultInputs = els.variablesGrid.querySelectorAll("[data-default-variable]");
        for (const input of defaultInputs) {
          const name = input.getAttribute("data-default-variable");
          if (name && message.variables && message.variables[name]) {
            message.variables[name].default = input.value;
          }
        }
        const variables = currentVariablesFromForm(message);
        const response = await fetch("/api/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            message,
            channel: els.channelSelect.value,
            variables
          })
        });
        const preview = await response.json();
        if (!response.ok) {
          throw new Error(preview.error || `Preview request failed (${response.status})`);
        }
        if (!preview.resolved || typeof preview.resolved !== "object") {
          throw new Error("Preview response did not contain rendered content.");
        }
        paintPreview(preview);
        renderChannelBlockControls(message);
        els.statusText.textContent = "Preview updated.";
      } catch (error) {
        els.statusText.textContent = "Preview error: " + error.message;
        paintPreviewError(error);
      }
    }

    function normalizeFormattedText(text, formats) {
      let value = String(text || "");
      const formatList = Array.isArray(formats) ? formats : [];
      if (formatList.includes("uppercase")) value = value.toUpperCase();
      if (formatList.includes("lowercase")) value = value.toLowerCase();
      if (formatList.includes("capitalize")) value = value.replace(/\\b\\w/g, (char) => char.toUpperCase());
      return value;
    }

    function renderSpanHtml(text, formats) {
      const value = escapeHtml(normalizeFormattedText(text, formats));
      const classNames = (Array.isArray(formats) ? formats : [])
        .filter((name) => ["bold", "italic", "underline", "strike", "code", "spoiler", "quote"].includes(name))
        .map((name) => `fmt-${name}`)
        .join(" ");
      if (!classNames) return value;
      return `<span class="${classNames}">${value}</span>`;
    }

    function renderBlockHtml(block) {
      if (!block || typeof block !== "object") return "";
      if (Array.isArray(block.parts) && block.parts.length) {
        const partsHtml = block.parts.map((part) => renderSpanHtml(part.text || "", part.format || [])).join("");
        const wrapperClasses = (Array.isArray(block.format) ? block.format : [])
          .filter((name) => ["bold", "italic", "underline", "strike", "code", "spoiler", "quote"].includes(name))
          .map((name) => `fmt-${name}`)
          .join(" ");
        return `<div class="message-block ${wrapperClasses}">${partsHtml}</div>`;
      }
      return `<div class="message-block">${renderSpanHtml(block.text || "", block.format || [])}</div>`;
    }

    function paintPreview(preview) {
      els.phoneChannel.textContent = preview.channel;
      els.deliveryBadge.textContent = preview.resolved.delivery || "plain_text";
      els.bindingBadge.textContent = preview.bindingText;

      const title = preview.resolved.title ? `<span class="message-title">${escapeHtml(preview.resolved.title)}</span>` : "";
      const blocks = resolvedContentBlocks(preview.resolved, preview.message.name || "message").map((block) => renderBlockHtml(block)).join("");
      els.bubble.className = "bubble";
      els.bubble.innerHTML = `${title}${blocks}` || '<span class="is-loading">This message has no visible content for the selected channel.</span>';

      els.actions.innerHTML = "";
      for (const action of (preview.resolved.actions || [])) {
        const button = document.createElement("div");
        button.className = "action";
        button.textContent = action.label + (action.live ? "" : " (not live)");
        els.actions.appendChild(button);
      }

      const variableFormats = [];
      for (const [name, config] of Object.entries(preview.message.variables || {})) {
        if (Array.isArray(config.format) && config.format.length) {
          variableFormats.push(`${name}: ${config.format.join(", ")}`);
        }
      }
      const blockFormats = Array.isArray(preview.detectedFormats) && preview.detectedFormats.length
        ? `Block formatting: ${preview.detectedFormats.join(", ")}`
        : "Block formatting: none";
      const variableLine = variableFormats.length
        ? `Variable formatting: ${variableFormats.join(" | ")}`
        : "Variable formatting: none";
      els.formats.textContent = `${blockFormats} | ${variableLine}`;

      const mediaAssets = normalizeAssets(preview.message && preview.message.assets).filter((asset) => asset.live);
      if (!mediaAssets.length) {
        els.assetGallery.innerHTML = '<div class="asset-empty">No accessible HTTPS media references are attached to this message.</div>';
        return;
      }
      const mediaMarkup = (asset) => {
        const url = escapeAttribute(asset.url);
        const alt = escapeAttribute(asset.alt || asset.assetId || `Message ${asset.type}`);
        if (asset.type === "image" || asset.type === "sticker") {
          return `<img class="asset-media" data-src="${url}" alt="${alt}" loading="lazy">`;
        }
        if (asset.type === "video") {
          return `<video class="asset-media" data-src="${url}" controls preload="metadata" aria-label="${alt}"></video>`;
        }
        if (asset.type === "audio") {
          return `<audio class="asset-media" data-src="${url}" controls preload="metadata" aria-label="${alt}"></audio>`;
        }
        return `<a class="asset-card-link asset-media" href="${url}" target="_blank" rel="noopener noreferrer">Open document</a>`;
      };
      els.assetGallery.innerHTML = mediaAssets.map((asset) => `
        <div class="asset-card" data-asset-id="${escapeAttribute(asset.assetId)}">
          ${mediaMarkup(asset)}
          <div class="asset-load-error">Media could not be loaded from its published URL.</div>
          <div class="asset-card-copy">
            <div class="asset-card-type">${escapeHtml(asset.type)}</div>
            <div class="asset-card-path">${escapeHtml(asset.assetId || "")}</div>
            <div class="asset-card-path">${escapeHtml(asset.url)}</div>
          </div>
        </div>
      `).join("");
      els.assetGallery.querySelectorAll("img.asset-media, video.asset-media, audio.asset-media").forEach((media) => {
        media.addEventListener("error", () => {
          const card = media.closest(".asset-card");
          if (card) card.classList.add("unavailable");
        }, { once: true });
        media.src = media.dataset.src || "";
        if (media.tagName === "AUDIO" || media.tagName === "VIDEO") media.load();
      });
    }

    function paintPreviewError(error) {
      els.deliveryBadge.textContent = "preview error";
      els.bindingBadge.textContent = "not rendered";
      els.bubble.className = "bubble is-error";
      els.bubble.textContent = `Could not render preview: ${error && error.message ? error.message : "unknown error"}`;
      els.actions.innerHTML = "";
      els.formats.textContent = "";
    }

    function addAssetReference() {
      try {
        const message = parseEditorMessage();
        if (!message || !message.name) throw new Error("Select a message before adding media.");
        const assetUrl = normalizeAssetUrl(els.assetUrlInput.value);
        if (!assetUrl) throw new Error("Enter a valid published HTTPS media URL.");
        const assetType = String(els.assetTypeSelect.value || "").trim().toLowerCase();
        const assetId = normalizeAssetKey(els.assetKeyInput.value, "", assetType);
        if (!String(els.assetKeyInput.value || "").trim()) throw new Error("Enter the Media Studio asset ID.");

        const assets = normalizeAssets(message.assets || []);
        const reference = {
          assetId,
          type: assetType,
          url: assetUrl,
          live: true,
          required: false
        };
        const existingIndex = assets.findIndex((asset) => asset.assetId === assetId);
        if (existingIndex >= 0) assets[existingIndex] = reference;
        else assets.push(reference);
        message.assets = assets;

        const normalized = normalizeMessage(message);
        writeEditorMessage(normalized);
        els.assetKeyInput.value = "";
        els.assetUrlInput.value = "";
        renderPreview();
        els.statusText.textContent = `Added media reference ${assetId}. Save the message to persist it.`;
      } catch (error) {
        els.statusText.textContent = "Media reference error: " + error.message;
      }
    }

    async function saveMessage() {
      try {
        const message = parseEditorMessage();
        if (!message) throw new Error("Message JSON is empty");
        writeEditorMessage(message);
        const response = await fetch("/api/messages/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            selected_name: state.selectedName,
            message
          })
        });
        if (!response.ok) {
          const payload = await response.json();
          throw new Error(payload.error || "Save failed");
        }
        els.statusText.textContent = "Saved to messages.json.";
        await loadCatalog();
        selectMessage(message.name);
      } catch (error) {
        els.statusText.textContent = "Save error: " + error.message;
      }
    }

    async function postToWebhook() {
      try {
        if (state.isDirty) {
          els.statusText.textContent = "Save the message first before notifying collectors.";
          return;
        }
        const response = await fetch("/api/export-webhook", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        });
        const responseText = await response.text();
        let payload = {};
        try {
          payload = responseText ? JSON.parse(responseText) : {};
        } catch {
          const contentType = response.headers.get("content-type") || "unknown content type";
          throw new Error(`Notify endpoint returned ${response.status} ${response.statusText || "response"} as ${contentType}, not JSON.`);
        }
        if (!response.ok) {
          const detail = payload.detail
            ? `${payload.error || "Collector notification failed"}: ${payload.detail}`
            : (payload.failed_count
              ? `${payload.failed_count} collector notification(s) failed`
              : (payload.error || "Collector notification failed"));
          throw new Error(detail);
        }
        els.statusText.textContent =
          `Notified ${payload.notified_count} collector(s) that their filtered exports are ready.`;
      } catch (error) {
        els.statusText.textContent = "Collector notification error: " + error.message;
      }
    }

    function resetEditor() {
      const message = getSelectedMessage();
      if (!message) return;
      const normalized = normalizeMessage(message);
      els.jsonEditor.value = JSON.stringify(normalized, null, 2);
      updateDirtyState();
      restoreSessionMessageView();
      renderComposer(normalized);
      renderVariableInputs(normalized);
      renderChannelBlockControls(normalized);
      renderPreview();
      els.statusText.textContent = "Editor reset.";
    }

    function setBlockRequiredForChannel(blockId, required) {
      try {
        const message = parseEditorMessage();
        if (!message) return;
        const channel = els.channelSelect.value;
        const variant = mergeChannelVariant(message, channel);
        const updatedBlocks = normalizeBlocks(message.name || "message", variant.blocks || []).map((block) => (
          block.id === blockId ? { ...block, required } : block
        ));
        message.overrides = message.overrides || {};
        message.overrides[channel] = {
          ...(message.overrides[channel] || {}),
          blocks: updatedBlocks
        };
        writeEditorMessage(message);
        renderChannelBlockControls(message);
        renderPreview();
        els.statusText.textContent = `Updated ${blockId} for ${channel}.`;
      } catch (error) {
        els.statusText.textContent = "Channel rule error: " + error.message;
      }
    }

    function addParagraphBlock() {
      try {
        const message = parseEditorMessage();
        if (!message) return;
        const variant = ensureComposerVariant(message);
        variant.blocks = normalizeBlocks(message.name || "message", variant.blocks || []);
        const newBlock = {
          id: nextParagraphId(message),
          type: "text",
          text: "New paragraph.",
          required: true
        };
        const numberedMenuIndex = variant.blocks.findIndex((block) => block.id === "numbered_menu");
        if (numberedMenuIndex >= 0) {
          variant.blocks.splice(numberedMenuIndex, 0, newBlock);
        } else {
          variant.blocks.push(newBlock);
        }
        writeEditorMessage(message);
        renderComposer(message);
        renderChannelBlockControls(message);
        renderPreview();
        els.statusText.textContent = "Added a new paragraph block.";
      } catch (error) {
        els.statusText.textContent = "Add paragraph error: " + error.message;
      }
    }

    function handleJsonInput() {
      try {
        const message = parseEditorMessage();
        if (!message) return;
        renderComposer(message);
        renderChannelBlockControls(message);
      } catch {
        els.composerParts.innerHTML = '<div class="composer-empty">Fix the JSON to resume composer editing.</div>';
        els.composerBlocks.innerHTML = '<div class="composer-empty">Fix the JSON to resume block editing.</div>';
        els.channelBlockList.innerHTML = '<div class="channel-block-note">Fix the JSON to manage channel block rules.</div>';
      }
      updateDirtyState();
      renderPreview();
    }

    function refreshPreviewAndRestoreView() {
      restoreSessionMessageView();
      renderPreview();
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    els.channelSelect.addEventListener("change", () => {
      try {
        const message = parseEditorMessage();
        if (message) {
          renderComposer(message);
          renderChannelBlockControls(message);
        }
      } catch {
        els.channelBlockList.innerHTML = '<div class="channel-block-note">Fix the JSON to manage channel block rules.</div>';
      }
      renderPreview();
    });
    els.groupSelect.addEventListener("change", handleGroupChange);
    els.categorySelect.addEventListener("change", handleCategoryChange);
    els.editGroupButton.addEventListener("click", editSelectedGroup);
    els.removeGroupButton.addEventListener("click", removeSelectedGroup);
    els.editCategoryButton.addEventListener("click", editSelectedCategory);
    els.removeCategoryButton.addEventListener("click", removeSelectedCategory);
    els.composerTitleInput.addEventListener("change", updateComposerTitle);
    els.actionsMenuEnabled.addEventListener("change", updateActionsMenu);
    els.actionsMenuInstruction.addEventListener("change", updateActionsMenu);
    els.actionsMenuItemFormat.addEventListener("change", updateActionsMenu);
    els.addBodyPartButton.addEventListener("click", addComposerTextPart);
    els.addVariablePartButton.addEventListener("click", addComposerVariablePart);
    els.addBlockButton.addEventListener("click", addComposerBlock);
    els.composerParts.addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const scope = target.getAttribute("data-part-scope");
      const textIndex = target.getAttribute("data-part-index");
      const blockIndex = target.getAttribute("data-block-index");
      if (scope === "body" && textIndex !== null && "value" in target) {
        updateComposerPartText(Number(textIndex), target.value);
      }
      if (scope === "block" && textIndex !== null && blockIndex !== null && "value" in target) {
        updateBlockComposerPartText(Number(blockIndex), Number(textIndex), target.value);
      }
    });
    function handleComposerClick(event) {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const removeScope = target.getAttribute("data-remove-part-scope");
      const removePartIndex = target.getAttribute("data-remove-part-index");
      const blockIndex = target.getAttribute("data-block-index");
      if (removeScope === "body" && removePartIndex !== null) {
        removeComposerPart(Number(removePartIndex));
        return;
      }
      if (removeScope === "block" && removePartIndex !== null && blockIndex !== null) {
        removeBlockComposerPart(Number(blockIndex), Number(removePartIndex));
        return;
      }
      const formatScope = target.getAttribute("data-format-scope");
      const formatIndex = target.getAttribute("data-part-index");
      const formatName = target.getAttribute("data-format-name");
      if (formatScope === "body" && formatIndex !== null && formatName) {
        toggleComposerPartFormat(Number(formatIndex), formatName);
        return;
      }
      if (formatScope === "block" && formatIndex !== null && blockIndex !== null && formatName) {
        toggleBlockComposerPartFormat(Number(blockIndex), Number(formatIndex), formatName);
        return;
      }
      const addBlockTextPart = target.getAttribute("data-add-block-text-part");
      if (addBlockTextPart !== null) {
        addBlockComposerTextPart(Number(addBlockTextPart));
        return;
      }
      const addBlockVariablePart = target.getAttribute("data-add-block-variable-part");
      if (addBlockVariablePart !== null) {
        addBlockComposerVariablePart(Number(addBlockVariablePart));
        return;
      }
      const removeBlock = target.getAttribute("data-remove-block");
      if (removeBlock !== null) {
        removeComposerBlock(Number(removeBlock));
        return;
      }
      const blockFormatIndex = target.getAttribute("data-block-format-index");
      const blockFormatName = target.getAttribute("data-block-format-name");
      if (blockFormatIndex !== null && blockFormatName) {
        toggleBlockComposerFormat(Number(blockFormatIndex), blockFormatName);
      }
    }
    els.composerParts.addEventListener("click", handleComposerClick);
    els.composerBlocks.addEventListener("click", handleComposerClick);
    els.composerBlocks.addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) return;
      const scope = target.getAttribute("data-part-scope");
      const textIndex = target.getAttribute("data-part-index");
      const blockIndex = target.getAttribute("data-block-index");
      if (scope === "block" && textIndex !== null && blockIndex !== null && "value" in target) {
        updateBlockComposerPartText(Number(blockIndex), Number(textIndex), target.value);
      }
    });
    els.jsonEditor.addEventListener("input", handleJsonInput);
    els.addAssetButton.addEventListener("click", addAssetReference);
    els.postWebhookButton.addEventListener("click", postToWebhook);
    els.duplicateButton.addEventListener("click", duplicateSelectedMessage);
    els.deleteButton.addEventListener("click", deleteSelectedMessage);
    els.saveButton.addEventListener("click", saveMessage);
    els.resetButton.addEventListener("click", resetEditor);
    els.formatButton.addEventListener("click", refreshPreviewAndRestoreView);
    els.newMessageButton.addEventListener("click", createNewMessage);
    els.addParagraphButton.addEventListener("click", addParagraphBlock);

    loadCatalog();
  </script>
</body>
</html>
"""


class MessageStudioHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.respond_html(INDEX_HTML)
            return

        if parsed.path.startswith("/public/"):
            self.respond_public_file(parsed.path)
            return

        if parsed.path.startswith("/api/export/collect/"):
            collector_id = normalize_collector_group_key(unquote(parsed.path[len("/api/export/collect/"):]))
            collector = load_collectors().get(collector_id)
            if not isinstance(collector, dict):
                self.respond_json({"error": "collector_not_found"}, status=HTTPStatus.NOT_FOUND)
                return
            if not is_collection_authorized(collector, self.headers.get("Authorization", "")):
                self.respond_json({"error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                return
            try:
                export_payload = load_saved_export_payload()
            except FileNotFoundError as error:
                self.respond_json({"error": str(error)}, status=HTTPStatus.NOT_FOUND)
                return
            self.respond_json(filter_export_for_collector(export_payload, collector, collector_id))
            return

        if parsed.path == "/api/catalog":
            catalog = load_catalog()
            self.respond_json({
                "taxonomy": catalog.get("taxonomy", {}),
                "messages": [hydrate_message(message) for message in catalog.get("messages", []) if isinstance(message, dict)]
            })
            return

        if parsed.path == "/api/message":
            query = parse_qs(parsed.query)
            name = query.get("name", [""])[0]
            catalog = load_catalog()
            message = find_message(catalog, name)
            if not message:
                self.respond_json({"error": "message_not_found"}, status=HTTPStatus.NOT_FOUND)
                return
            self.respond_json(hydrate_message(message))
            return

        self.respond_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/taxonomy":
            payload = self.read_json()
            action = str(payload.get("action", "")).strip()
            group = str(payload.get("group", "")).strip()
            category = str(payload.get("category", "") or "").strip()
            new_key = slugify(payload.get("new_key", "")) if payload.get("new_key") else ""
            catalog = load_catalog()
            taxonomy = catalog.get("taxonomy", {})
            messages = catalog.get("messages", [])

            if action == "create_group" and group:
                taxonomy.setdefault(group, [""])
            elif action == "create_category" and group and new_key:
                taxonomy.setdefault(group, [""])
                taxonomy[group] = list(dict.fromkeys([*taxonomy[group], new_key]))
            elif action == "rename_group" and group and new_key:
                if new_key != group and new_key in taxonomy:
                    self.respond_json({"error": "group_name_exists"}, status=HTTPStatus.CONFLICT)
                    return
                categories = taxonomy.pop(group, [""])
                taxonomy[new_key] = list(dict.fromkeys([*taxonomy.get(new_key, []), *categories]))
                for message in messages:
                    if message.get("group") == group:
                        message["group"] = new_key
            elif action == "remove_group" and group:
                remaining_groups = [key for key in taxonomy if key != group]
                if not remaining_groups:
                    self.respond_json({"error": "cannot_remove_last_group"}, status=HTTPStatus.BAD_REQUEST)
                    return
                fallback_group = str(payload.get("fallback_group", "")).strip()
                if fallback_group not in remaining_groups:
                    fallback_group = remaining_groups[0]
                taxonomy[fallback_group] = list(dict.fromkeys([
                    *taxonomy.get(fallback_group, [""]),
                    *taxonomy.get(group, []),
                ]))
                taxonomy.pop(group, None)
                for message in messages:
                    if message.get("group") == group:
                        message["group"] = fallback_group
            elif action == "rename_category" and group and category and new_key:
                categories = taxonomy.setdefault(group, [""])
                taxonomy[group] = [new_key if item == category else item for item in categories]
                taxonomy[group] = list(dict.fromkeys(taxonomy[group]))
                for message in messages:
                    if message.get("group") == group and message.get("category", "") == category:
                        message["category"] = new_key
            elif action == "remove_category" and group and category:
                taxonomy[group] = [item for item in taxonomy.get(group, [""]) if item != category]
                for message in messages:
                    if message.get("group") == group and message.get("category", "") == category:
                        message["category"] = ""
            else:
                self.respond_json({"error": "invalid_taxonomy_action"}, status=HTTPStatus.BAD_REQUEST)
                return

            catalog["taxonomy"] = taxonomy
            save_catalog(catalog)
            saved = load_catalog()
            self.respond_json({
                "ok": True,
                "taxonomy": saved.get("taxonomy", {}),
                "messages": [hydrate_message(message) for message in saved.get("messages", [])],
            })
            return

        if parsed.path == "/api/preview":
            payload = self.read_json()
            message = normalize_message_shape(payload.get("message", {}))
            channel = str(payload.get("channel", "telegram"))
            variables = payload.get("variables", {})
            resolved = resolve_message(message, channel, variables)
            binding = message.get("overrides", {}).get(channel, {}).get("binding", {})
            binding_text = "no binding"
            if binding:
                pieces = [f"{key}={value}" for key, value in binding.items()]
                binding_text = ", ".join(pieces)
            self.respond_json({
                "message": hydrate_message(message),
                "channel": channel,
                "resolved": resolved,
                "bindingText": binding_text,
                "detectedFormats": collect_formats(resolved),
            })
            return

        if parsed.path == "/api/messages/save":
            payload = self.read_json()
            selected_name = str(payload.get("selected_name", "")).strip()
            message = normalize_message_shape(payload.get("message", {}))
            if not selected_name:
                self.respond_json({"error": "selected_name_required"}, status=HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(message, dict) or not str(message.get("name", "")).strip():
                self.respond_json({"error": "message_name_required"}, status=HTTPStatus.BAD_REQUEST)
                return

            catalog = load_catalog()
            messages = catalog.get("messages", [])
            for index, existing in enumerate(messages):
                if existing.get("name") == selected_name:
                    messages[index] = message
                    save_catalog(catalog)
                    export_payload = load_saved_export_payload()
                    self.respond_json({
                        "ok": True,
                        "message": hydrate_message(message),
                        "export_path": str(EXPORT_PAYLOAD_PATH),
                        "exported_count": len(export_payload.get("messages", [])),
                    })
                    return

            self.respond_json({"error": "message_not_found"}, status=HTTPStatus.NOT_FOUND)
            return

        if parsed.path == "/api/messages/create":
            payload = self.read_json()
            message = normalize_message_shape(payload.get("message", {}))
            if not isinstance(message, dict):
                self.respond_json({"error": "message_payload_required"}, status=HTTPStatus.BAD_REQUEST)
                return

            name = str(message.get("name", "")).strip()
            if not name:
                self.respond_json({"error": "message_name_required"}, status=HTTPStatus.BAD_REQUEST)
                return

            catalog = load_catalog()
            messages = catalog.get("messages", [])
            if any(existing.get("name") == name for existing in messages):
                self.respond_json({"error": "message_name_exists"}, status=HTTPStatus.CONFLICT)
                return

            messages.append(message)
            save_catalog(catalog)
            export_payload = load_saved_export_payload()
            self.respond_json({
                "ok": True,
                "message": hydrate_message(message),
                "export_path": str(EXPORT_PAYLOAD_PATH),
                "exported_count": len(export_payload.get("messages", [])),
            }, status=HTTPStatus.CREATED)
            return

        if parsed.path == "/api/messages/delete":
            payload = self.read_json()
            selected_name = str(payload.get("selected_name", "")).strip()
            if not selected_name:
                self.respond_json({"error": "selected_name_required"}, status=HTTPStatus.BAD_REQUEST)
                return

            catalog = load_catalog()
            messages = catalog.get("messages", [])
            delete_index = next((index for index, existing in enumerate(messages) if existing.get("name") == selected_name), -1)
            if delete_index < 0:
                self.respond_json({"error": "message_not_found"}, status=HTTPStatus.NOT_FOUND)
                return

            deleted = messages.pop(delete_index)
            save_catalog(catalog)
            export_payload = load_saved_export_payload()

            next_selected_name = ""
            if messages:
                next_index = min(delete_index, len(messages) - 1)
                next_selected_name = str(messages[next_index].get("name", ""))

            self.respond_json({
                "ok": True,
                "deleted_name": deleted.get("name", selected_name),
                "next_selected_name": next_selected_name,
                "export_path": str(EXPORT_PAYLOAD_PATH),
                "exported_count": len(export_payload.get("messages", [])),
            })
            return

        if parsed.path == "/api/export-webhook":
            try:
                export_payload = load_saved_export_payload()
                collector_results = notify_export_collectors(export_payload)
            except FileNotFoundError as error:
                self.respond_json({"error": str(error)}, status=HTTPStatus.CONFLICT)
                return
            except ValueError as error:
                self.respond_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return

            notified_count = sum(1 for result in collector_results if result.get("ok"))
            failed_count = len(collector_results) - notified_count

            self.respond_json({
                "ok": failed_count == 0,
                "exported_count": len(export_payload.get("messages", [])),
                "notified_count": notified_count,
                "failed_count": failed_count,
                "collectors": collector_results,
                "export_path": str(EXPORT_PAYLOAD_PATH),
            }, status=HTTPStatus.OK if failed_count == 0 else HTTPStatus.BAD_GATEWAY)
            return

        self.respond_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/messages/"):
            self.respond_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return

        selected_name = unquote(parsed.path.split("/")[-1])
        payload = normalize_message_shape(self.read_json())
        catalog = load_catalog()
        messages = catalog.get("messages", [])

        for index, message in enumerate(messages):
            if message.get("name") == selected_name:
                messages[index] = payload
                save_catalog(catalog)
                export_payload = load_saved_export_payload()
                self.respond_json({
                    "ok": True,
                    "message": hydrate_message(payload),
                    "export_path": str(EXPORT_PAYLOAD_PATH),
                    "exported_count": len(export_payload.get("messages", [])),
                })
                return

        self.respond_json({"error": "message_not_found"}, status=HTTPStatus.NOT_FOUND)

    def read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
        return json.loads(raw or "{}")

    def read_multipart_form(self) -> MultipartForm:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("multipart_form_required")

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b""
        message = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )
        if not message.is_multipart():
            raise ValueError("invalid_multipart_form")

        form = MultipartForm()
        for part in message.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            if not field_name:
                continue
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is not None:
                form.fields[field_name] = MultipartUpload(
                    filename=filename,
                    content_type=part.get_content_type(),
                    content=payload,
                )
            else:
                charset = part.get_content_charset() or "utf-8"
                form.fields[field_name] = payload.decode(charset, errors="replace")
        return form

    def respond_public_file(self, request_path: str) -> None:
        relative_path = request_path[len("/public/"):].replace("\\", "/").strip("/")
        if not relative_path:
            self.respond_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        file_path = (PUBLIC_DIR / relative_path).resolve()
        try:
            file_path.relative_to(PUBLIC_DIR.resolve())
        except ValueError:
            self.respond_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        if not file_path.exists() or not file_path.is_file():
            self.respond_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return
        body = file_path.read_bytes()
        content_type, _ = mimetypes.guess_type(str(file_path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), MessageStudioHandler)
    print(f"Message Studio running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
