"""stack-doctor checks package.

This module explicitly re-exports the check entry points and the small number
of auxiliary functions used by the rest of the package. The wildcard exports
from individual submodules are no longer re-exported here, so the public surface
of doctor.checks is now well-defined.
"""
from .queue import check_queue
from .providers import check_providers
from .decypharr import check_decypharr
from .decypharr_providers import check_decypharr_providers
from .plex import check_plex
from .plexscan import check_plex_scan
from .rescan import check_rescan
from .resources import check_resources
from .janitor import check_janitor
from .bazarr import check_bazarr
from .seerr import check_seerr
from .repair import check_repair
from .debridlink_migration import check_debridlink_migration
from .force_import import check_force_import
from .warmer import warmer_loop, plexlog_loop
from .missing_seasons import check_missing_seasons, backfill_missing_seasons
from .no_upgrade import check_no_upgrade_profile
from .multipack import check_multipack
from .maintainer import check_maintainer

__all__ = [
    "check_bazarr",
    "check_decypharr",
    "check_decypharr_providers",
    "check_debridlink_migration",
    "check_force_import",
    "check_janitor",
    "check_maintainer",
    "check_missing_seasons",
    "check_maintainer",
    "check_multipack",
    "check_no_upgrade_profile",
    "check_plex",
    "check_plex_scan",
    "check_providers",
    "check_rescan",
    "check_queue",
    "check_repair",
    "check_resources",
    "check_seerr",
    "backfill_missing_seasons",
    "warmer_loop",
    "backfill_missing_seasons",
    "check_maintainer",
    "warmer_loop",
    "plexlog_loop",
]
