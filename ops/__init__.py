"""Operational concerns: alerting, run metadata, secret redaction, resilience.

Imported as ``from ops.<module> import ...``. This package deliberately does not
re-export its members: ``ops.pipeline_run`` pulls in the storage layer, and a
collector that only wants ``ops.resilience`` should not pay for — or be able to
break on — a Hub or database import.
"""
