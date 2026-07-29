"""Source adapters.

Each adapter turns one kind of website into `CampSession` objects. Adapters are
registered by name in `REGISTRY` and referenced from `config/sources.yaml`, so
adding a source is a config change plus (sometimes) one new module.
"""

from .activesearch import ActiveSearchAdapter
from .base import Adapter, AdapterError
from .jsonld import JsonLdAdapter
from .tribe import TribeEventsAdapter

#: Maps the `adapter:` field in sources.yaml to an implementation.
REGISTRY: dict[str, type[Adapter]] = {
    "activesearch": ActiveSearchAdapter,
    "jsonld": JsonLdAdapter,
    "tribe": TribeEventsAdapter,
}

__all__ = [
    "ActiveSearchAdapter",
    "Adapter",
    "AdapterError",
    "JsonLdAdapter",
    "REGISTRY",
    "TribeEventsAdapter",
]
