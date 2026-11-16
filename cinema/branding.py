"""Allowlisted instance branding; never apply instance-provided CSS."""
import re
from urllib.parse import urljoin, urlsplit


def asset_url(value, origin):
    if not isinstance(value, str) or not value.strip():
        return None
    if re.search(r"[\s'\"<>;\\]", value):
        return None
    try:
        value = urljoin(origin + "/", value.strip())
        parsed = urlsplit(value)
    except ValueError:
        return None
    schemes = {"https"} if origin.startswith("https:") else {"https", "http"}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def branding(document, origin):
    public = document.get("app_public") or {}
    source = public.get("branding") if isinstance(public, dict) else None
    source = source if isinstance(source, dict) else {}
    name = source.get("product_name")
    name = name.strip()[:100] if isinstance(name, str) and name.strip() else urlsplit(origin).hostname
    color = source.get("theme_color")
    color = color.lower() if isinstance(color, str) and re.fullmatch(r"#[a-fA-F0-9]{6}", color) else "#4641d9"
    def asset(*keys):
        return next((url for key in keys if (url := asset_url(source.get(key), origin))), None)
    return {"name": name, "color": color, "logo": asset("logo_url", "wordmark_url"),
            "icon": asset("icon_url", "symbol_url"), "favicon": asset("favicon_url", "icon_url", "symbol_url"),
            "favicon_dark": asset("favicon_dark_url"), "backdrop": asset("sign_in_backdrop_url")}
