"""Explicit compatibility for the verified carrier model/basis transition."""
from copy import deepcopy
from .jig_heading import mesh_yaw_offset

LEGACY_CARRIER_SHA='959603f107b9287a4e59c7a7108fac58d0f95c1df742ddcd4b31bd9aa07b8172'
PREVIOUS_ASSEMBLED_CARRIER_SHA='b93ee6bec1eb2bded3d2503dcab513d3ce82a1a134a8c354d8643b8a3e2b41e7'
OCT02_ASSEMBLED_CARRIER_SHA='06a99f0b1ae1d0132649333b2e97f4eafd58f62d1eb7b52793ee66da6022a514'
ASSEMBLED_CARRIER_SHA='23f8ddb6d925c8c15f5ec543804dc42c2db10bc356cfcdaa45742b1c0e04f4d9'

def execution_reference(reference,current,mesh_sha):
    # Both verified assets use [-110,-74] .. [110,74] mm in the same base
    # XY frame. The assembly adds the existing six fixtures, not a new taught
    # TCP or support height. Legacy headings use the retained A-facing +X.
    if (not reference or not current or mesh_sha!=ASSEMBLED_CARRIER_SHA
            or current.get('stl_sha256')!=ASSEMBLED_CARRIER_SHA
            or current.get('symmetry_deg')!=360):return reference
    if mesh_yaw_offset(current)!=180:return reference
    saved=reference.get('stl_sha256');symmetry=reference.get('symmetry_deg');offset=mesh_yaw_offset(reference)
    legacy=saved in (LEGACY_CARRIER_SHA,PREVIOUS_ASSEMBLED_CARRIER_SHA,OCT02_ASSEMBLED_CARRIER_SHA,ASSEMBLED_CARRIER_SHA) and symmetry==180 and offset==0
    # B carrier fixtures changed, with taller lower walls on 2026-10-11.
    # Verified CAD product seating, base XY, rim, six centres and the A
    # heading feature remain unchanged. Saved motion is never rewritten.
    previous=saved in (PREVIOUS_ASSEMBLED_CARRIER_SHA,OCT02_ASSEMBLED_CARRIER_SHA) and symmetry==360 and offset==180
    if not (legacy or previous):return reference
    resolved=deepcopy(reference)
    resolved.update(stl_sha256=ASSEMBLED_CARRIER_SHA,symmetry_deg=360,mesh_yaw_offset_deg=180.)
    return resolved
