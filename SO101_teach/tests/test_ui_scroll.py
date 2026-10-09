import tkinter as tk
from tkinter import ttk
import unittest
from so101_teach.ui_scroll import AutoScrollbar

class AutoScrollbarTests(unittest.TestCase):
    def test_overflow_scrolls_and_short_content_reclaims_space(self):
        root=tk.Tk();root.geometry('360x220')
        try:
            root.columnconfigure(0,weight=1);root.rowconfigure(0,weight=1)
            tree=ttk.Treeview(root,columns=('name',),show='headings');tree.grid(row=0,column=0,sticky='nsew')
            bar=AutoScrollbar(root,command=tree.yview);bar.grid(row=0,column=1,sticky='ns');tree.configure(yscrollcommand=bar.set)
            root.update();self.assertFalse(bar.winfo_ismapped())
            for i in range(40):tree.insert('','end',values=(str(i),))
            root.update();self.assertTrue(bar.winfo_ismapped())
            tree.yview_moveto(1);root.update();self.assertGreater(tree.yview()[0],0)
            tree.delete(*tree.get_children());root.update();self.assertFalse(bar.winfo_ismapped());self.assertEqual(tree.yview(),(0.,1.))
        finally:root.destroy()
