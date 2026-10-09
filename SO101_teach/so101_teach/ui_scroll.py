"""Grid scrollbars appear only when content exceeds its viewport."""
from tkinter import ttk


class AutoScrollbar(ttk.Scrollbar):
    def set(self, first, last):
        visible = float(first) > .00001 or float(last) < .99999
        if visible != bool(self.grid_info()):
            self.grid() if visible else self.grid_remove()
        super().set(first, last)
