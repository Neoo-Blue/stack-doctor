"""HTTP API clients package: Arr (Sonarr/Radarr/Prowlarr), Plex, Seerr."""
from .arr import Arr
from .plex import Plex
from .seerr import Seerr
from .decypharr import Decypharr
from .tautulli import Tautulli
from .pulsarr import Pulsarr
from .loader import load_instances, INSTANCES

__all__ = ["Arr", "Plex", "Seerr", "Decypharr", "Tautulli", "Pulsarr", "load_instances", "INSTANCES"]
