"""Source adapters.

Each adapter turns one kind of website into `CampSession` objects. Adapters are
registered by name in `REGISTRY` and referenced from `config/sources.yaml`, so
adding a source is a config change plus (sometimes) one new module.
"""

from .base import Adapter, AdapterError
from .jsonld import JsonLdAdapter

#: Maps the `adapter:` field in sources.yaml to an implementation.
REGISTRY: dict[str, type[Adapter]] = {
    "jsonld": JsonLdAdapter,
}

__all__ = ["Adapter", "AdapterError", "JsonLdAdapter", "REGISTRY"]
