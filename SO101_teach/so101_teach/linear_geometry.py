"""Shared linear carriage coordinates for inspection and desktop display."""

def carriage_position_mm(stage):
    """Map command values to the camera-confirmed rigid carriage endpoints."""
    stroke=float(stage['stroke_mm']);endpoints=stage.get('endpoint_reference',{})
    forward=endpoints.get('forward',{});retracted=endpoints.get('retracted',{})
    if 'carriage_x_mm' in forward and 'carriage_x_mm' in retracted:
        fraction=(stroke-retracted['commanded_mm'])/(forward['commanded_mm']-retracted['commanded_mm'])
        return retracted['carriage_x_mm']+fraction*(forward['carriage_x_mm']-retracted['carriage_x_mm'])
    return stroke+stage.get('carriage_home_x_mm',-50.)
