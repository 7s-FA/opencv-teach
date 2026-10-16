"""Stable, scrollable status text; message length never resizes its neighbours."""
import tkinter as tk
from tkinter import ttk
from .ui_theme import COLORS


def set_text(variable,text):
    if variable.get()!=text:variable.set(text)


class StatusText(ttk.Frame):
    def __init__(self,parent,variable,*,height=3,font=('Noto Sans CJK KR',10),background=None,foreground=None):
        super().__init__(parent,style='Card.TFrame')
        self.columnconfigure(0,weight=1);self.rowconfigure(0,weight=1)
        self.variable=variable
        self.text=tk.Text(self,height=height,width=1,font=font,foreground=foreground or COLORS['muted'],background=background or COLORS['panel'],
                          wrap='word',relief='flat',borderwidth=0,highlightthickness=0,state='disabled',padx=0,pady=0)
        self.text.grid(row=0,column=0,sticky='nsew')
        def wheel(amount):
            self.text.yview_scroll(amount,'units');return 'break'
        self.text.bind('<Button-4>',lambda e:wheel(-3));self.text.bind('<Button-5>',lambda e:wheel(3))
        self.text.bind('<MouseWheel>',lambda e:wheel(-max(1,abs(int(e.delta/120))) if e.delta>0 else max(1,abs(int(e.delta/120)))))
        self.trace=variable.trace_add('write',self.refresh)
        self.bind('<Destroy>',self.dispose,add='+');self.refresh()
    def refresh(self,*_):
        value=self.variable.get()
        if self.text.get('1.0','end-1c')==value:return
        self.text.configure(state='normal');self.text.delete('1.0','end');self.text.insert('1.0',value)
        self.text.configure(state='disabled');self.text.yview_moveto(0)
    def dispose(self,event):
        if event.widget is self and self.trace:
            self.variable.trace_remove('write',self.trace);self.trace=None
