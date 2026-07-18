"""Platform instrument-catalog layer.

Separates the *underlying market instrument* from the *binary-options
contract*: only verified catalog entries can make a row actionable. See
docs/binary_options/instrument-catalog.md.
"""
from .models import (  # noqa: F401
    CONTRACT_CLOSED,
    CONTRACT_OPEN,
    CONTRACT_STATUSES,
    CONTRACT_SUSPENDED,
    SUPPORTED_ASSET_TYPES,
    BinaryInstrument,
    CatalogSnapshot,
)
from .providers import (  # noqa: F401
    BinaryInstrumentCatalogProvider,
    CatalogError,
    CsvCatalogProvider,
    YamlCatalogProvider,
    build_catalog_provider,
)
from .validation import CatalogValidationResult, validate_catalog  # noqa: F401
