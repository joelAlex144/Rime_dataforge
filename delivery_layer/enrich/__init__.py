"""Build-time enrichment of fixtures: overview, section briefs, clause tags,
suggested questions, topic chips, table descriptions.

Nothing here runs in the judged path. `scripts/enrich.py` calls a provider at
fixture-preparation time and writes the generated fields into the fixture,
each marked `generated: true` with the provider's name; the reader only reads
the fixture. With ENRICH_PROVIDER=none (the default) nothing is generated and
the reader uses the mechanical document map.
"""
from .provider import EnrichProvider, NoneProvider, ProviderError, make_enrich_provider  # noqa: F401
