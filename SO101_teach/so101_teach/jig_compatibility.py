"""Explicit compatibility for the verified carrier model/basis transition."""
from copy import deepcopy
from .jig_heading import mesh_yaw_offset

LEGACY_CARRIER_SHA='959603f107b9287a4e59c7a7108fac58d0f95c1df742ddcd4b31bd9aa07b8172'
PREVIOUS_ASSEMBLED_CARRIER_SHA='b93ee6bec1eb2bded3d2503dcab513d3ce82a1a134a8c354d8643b8a3e2b41e7'
ASSEMBLED_CARRIER_SHA='06a99f0b1ae1d0132649333b2e97f4eafd58f62d1eb7b52793ee66da6022a514'

def execution_reference(reference,current,mesh_sha):
    # Both verified assets use [-110,-74] .. [110,74] mm in the same base
    # XY frame. The assembly adds the existing six fixtures, not a new taught
    # TCP or support height. Legacy headings use the retained A-facing +X.
    if (not reference or not current or mesh_sha!=ASSEMBLED_CARRIER_SHA
            or current.get('stl_sha256')!=ASSEMBLED_CARRIER_SHA
            or current.get('symmetry_deg')!=360):return reference
    if mesh_yaw_offset(current)!=180:return reference
    saved=reference.get('stl_sha256');symmetry=reference.get('symmetry_deg');offset=mesh_yaw_offset(reference)
    legacy=saved in (LEGACY_CARRIER_SHA,PREVIOUS_ASSEMBLED_CARRIER_SHA,ASSEMBLED_CARRIER_SHA) and symmetry==180 and offset==0
    # B upper fixture changed internally; base XY, rim, six seats and the A
    # orientation feature are exactly unchanged in the verified two assemblies.
    previous=saved==PREVIOUS_ASSEMBLED_CARRIER_SHA and symmetry==360 and offset==180
    if not (legacy or previous):return reference
    resolved=deepcopy(reference)
    resolved.update(stl_sha256=ASSEMBLED_CARRIER_SHA,symmetry_deg=360,mesh_yaw_offset_deg=180.)
    return resolved
