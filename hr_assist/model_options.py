"""Documented, explicit GLM-5.3 generation options.

Default settings add no wire fields and preserve legacy cache keys. Provider
options are intentionally restricted to the verified standard API, rather than
forwarded as arbitrary extra parameters to unrelated compatible endpoints.
"""
from urllib.parse import urlsplit

from .storage import BusinessError


EFFORTS = frozenset({"default", "low", "high", "max"})
MIN_OUTPUT_TOKENS = 1024
MAX_OUTPUT_TOKENS = 131072


def generation_profile(settings):
    """Return only explicit, validated options, with no endpoint or secrets."""
    effort = settings.get("reasoning_effort", "default")
    limit = settings.get("max_tokens")
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise BusinessError("MODEL_REQUEST", "思考强度配置无效，请选择沿用默认、low、high 或 max。GLM-5.3 不支持关闭思考。")
    if limit is not None and (type(limit) is not int or not MIN_OUTPUT_TOKENS <= limit <= MAX_OUTPUT_TOKENS):
        raise BusinessError("MODEL_REQUEST", "最大输出 token 必须留空或填写 1024—131072 之间的整数。")
    profile = {}
    if effort != "default":
        profile["reasoning_effort"] = effort
    if limit is not None:
        profile["max_tokens"] = limit
    if not profile:
        return profile
    try:
        endpoint = urlsplit(settings.get("endpoint", ""))
        supported = (
            settings.get("model") == "glm-5.3"
            and endpoint.scheme == "https" and endpoint.hostname == "open.bigmodel.cn"
            and endpoint.port in (None, 443)
            and endpoint.path in {"/api/paas/v4/chat/completions", "/api/paas/v4/chat/completions/"}
            and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment
        )
    except (ValueError, TypeError, AttributeError):
        supported = False
    if not supported:
        raise BusinessError("MODEL_REQUEST", "思考强度与输出上限目前仅支持智谱标准接口的 glm-5.3；请核对接口和模型，或将思考强度恢复默认并清空输出上限。")
    return profile


def request_options(settings):
    """Map validated settings to the provider's documented request fields."""
    profile = generation_profile(settings)
    options = dict(profile)
    if "reasoning_effort" in profile:
        options["thinking"] = {"type": "enabled"}
    return options
