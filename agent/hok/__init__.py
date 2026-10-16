"""Honor of Kings 5v5: visible-state team rules (agent/hok/policy.py)."""

from .policy import GAME_TYPE, HokPolicy, is_hok_shape, safe_fallback_action

__all__ = ["GAME_TYPE", "HokPolicy", "is_hok_shape", "safe_fallback_action"]
