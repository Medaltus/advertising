"""
Compatibility shim — the real implementation is ../fetch_total_sales.py.

WHY THIS FILE IS NOT THE IMPLEMENTATION
---------------------------------------
This used to be a full second copy of fetch_total_sales.py, and it had drifted
badly behind the root one. Most importantly it had no 429 handling at all:

    resp = requests.post(...)          # this file, before
    resp = _request_with_backoff(...)  # root file, 6 retries honouring Retry-After

Python puts a script's OWN directory first on sys.path, so
`python3 skinuva/generate_skinuva_supplement.py` imported THIS copy, not the
maintained one. Amazon rate-limits createReport per account, and the Medaltus
pipeline spends part of that quota earlier in the same workflow run — so the
Skinuva backfill regularly met 429 QuotaExceeded and, with no retry, gave up
instantly. Each of those instant failures wiped a month's total sales.

Observed in run 31500966243: June and July both failed with
"429 QuotaExceeded" on the very first attempt, no retry, no wait.

Re-exporting keeps every existing `from fetch_total_sales import ...` in this
directory working while guaranteeing there is exactly one implementation to
maintain. Loaded by explicit path under a different module name because
importing 'fetch_total_sales' from here would just find this file again.
"""

import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent / 'fetch_total_sales.py'
if not _ROOT.exists():                                    # pragma: no cover
    raise ImportError(f'fetch_total_sales implementation not found at {_ROOT}')

_spec = _ilu.spec_from_file_location('_fetch_total_sales_root', _ROOT)
_mod = _ilu.module_from_spec(_spec)
_sys.modules['_fetch_total_sales_root'] = _mod
_spec.loader.exec_module(_mod)

# Re-export the public surface (functions, constants) of the real module.
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith('__')})

__all__ = [k for k in vars(_mod) if not k.startswith('_')]
