"""Presentation axes for the current overhead mount. Internal base frame unchanged.

Display +X is robot right (-base Y), +Y is robot forward (+base X).
The origin stays at base_link, not at an image corner. A 3D view orbit does not
rotate these numerical axes. TCP local configuration offsets are not converted.
"""

def display_position(position):
    return (-position[1],position[0],*position[2:])


def display_heading(degrees,symmetry=360):
    return (degrees+90)%symmetry


def display_pose(pose,symmetry=360):
    x,y=display_position(pose[:2])
    return (x,y,display_heading(pose[2],symmetry))


def display_delta(delta):
    # A change in heading is independent of the fixed choice of XY basis.
    return (-delta[1],delta[0],delta[2])
