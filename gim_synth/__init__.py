"""Synthetic Genesys Info Mart (INTERACTION_RESOURCE_FACT style) voice call data generator."""
from .config import GeneratorConfig, default_config, load_config
from .generator import Generator, generate

__all__ = ["GeneratorConfig", "Generator", "default_config", "generate", "load_config"]
__version__ = "0.1.0"
