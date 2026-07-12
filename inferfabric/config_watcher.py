"""
inferfabric/config_watcher.py — Config hash computation and drift detection.

Extracted from manager.py (v4.1 P3): pure-function module for config
integrity monitoring.
"""

import logging

log = logging.getLogger("inferfabric")


def compute_config_hash(model) -> str:
    """Compute deterministic hash of model config.

    Delegates to model.config_hash() which hashes all startup-affecting
    fields while excluding runtime-only fields (e.g. typical_vram_pct).

    Args:
        model: ModelConfig instance with config_hash() method.

    Returns:
        Hex digest string.
    """
    return model.config_hash()


def detect_drift(model, state) -> bool:
    """Check if model config has drifted from stored hash.

    Args:
        model: ModelConfig instance.
        state: StateDB instance with get() method.

    Returns:
        True if config has changed since last deployment.
    """
    current_hash = compute_config_hash(model)
    stored_hash = state.get(f"config_hash:{model.name}")

    if stored_hash is None:
        log.info("Config hash for %s not found, recording: %s", model.name, current_hash)
        state.set(f"config_hash:{model.name}", current_hash)
        return False

    if stored_hash != current_hash:
        log.info("Config drift detected for %s: stored=%s current=%s",
                 model.name, stored_hash, current_hash)
        return True

    return False


def reload_and_check(models_dir, model_name, state) -> bool:
    """Reload models from disk, re-lookup model, and check for drift.

    Returns True if drift detected (model should be restarted).
    Returns False if no drift or error occurred.

    Args:
        models_dir: Path to models YAML directory.
        model_name: Name of model to check.
        state: StateDB instance.
    """
    from .config import load_models
    models = load_models(models_dir)
    model = models.get(model_name)
    if model is None:
        log.warning("YAML for %s not found after reload — skipping drift check", model_name)
        return False

    return detect_drift(model, state)