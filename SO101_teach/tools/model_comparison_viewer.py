"""Open the saved-photo four-model comparison without camera or GPU access."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def main():
    import tkinter as tk
    from so101_teach.ui import styles,show_ready_window
    from so101_teach.yolo_ui import ComparisonPanel
    root=tk.Tk();root.withdraw();root.title('SO-101 · 네 모델 사진 비교')
    root.geometry('1920x1043');root.minsize(1000,700);styles(root)
    ComparisonPanel(root,ROOT/'data/model-comparison/index.json').pack(fill='both',expand=True)
    show_ready_window(root);root.mainloop()

if __name__=='__main__':main()
