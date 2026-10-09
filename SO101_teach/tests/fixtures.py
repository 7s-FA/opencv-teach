"""Frozen calibration for regressions; never use or change the operator profile."""
from pathlib import Path
from so101_teach.domain import load_profile as _load_profile
DATA=Path(__file__).parent/'fixtures'
def load_profile(data_dir=None):
    return _load_profile(DATA if data_dir is None else data_dir)

def legacy_carrier_episode():
    """Exercise legacy compatibility independently of corrected operator records."""
    import json
    from so101_teach.domain import ROOT
    from so101_teach.jig_compatibility import LEGACY_CARRIER_SHA
    episode=json.loads((DATA/'carrier-episode.json').read_text())
    key='b5ebdb54807c441c8267c15d77713b6a'
    references=[episode['jig_references'][key]]+[s['jig_reference'] for s in episode['steps'] if s.get('jig_id')==key]
    for reference in references:
        reference.update(stl_sha256=LEGACY_CARRIER_SHA,symmetry_deg=180)
        reference.pop('mesh_yaw_offset_deg',None)
    return episode
