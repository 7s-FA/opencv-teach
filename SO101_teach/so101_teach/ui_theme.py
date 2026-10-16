"""Soft teal surfaces drawn once; ttk retains native input and keyboard behavior."""
from tkinter import ttk
from PIL import Image, ImageDraw, ImageTk

COLORS = {'bg': '#dbe3e8', 'panel': '#edf2f4', 'ink': '#20323e',
          'muted': '#465c69', 'border': '#6d8795', 'nav': '#142d38',
          'accent': '#087c86', 'selected': '#c9e2e5', 'danger': '#ad333d',
          'viewport': '#d4dfe5', 'field': '#f7fafb', 'button': '#dbe7ec',
          'heading': '#cbd9e1', 'panel_border': '#a6bac5'}


def install(root):
    """Install image elements in the current clam theme, with one owner per Tk."""
    owner = root._root()
    if getattr(owner, 'so101_theme_images', None):
        return
    images = owner.so101_theme_images = []
    style = ttk.Style(root)

    def shape(fill, outline=None, radius=8, size=(26, 26), inset=1):
        scale = 3
        im = Image.new('RGBA', (size[0]*scale, size[1]*scale))
        draw = ImageDraw.Draw(im)
        draw.rounded_rectangle((inset*scale, inset*scale,
                                (size[0]-inset)*scale-1, (size[1]-inset)*scale-1),
                               radius=radius*scale, fill=fill, outline=outline,
                               width=scale if outline else 0)
        photo = ImageTk.PhotoImage(im.resize(size, Image.Resampling.LANCZOS), master=owner)
        images.append(photo)
        return photo

    def surface(name, fill, outline=None, states=(), radius=4, size=(22, 22), border=10):
        # Tk tiles the image center; it does not scale it. A 2px center creates
        # hundreds of thousands of X image copies on each full-window repaint.
        # Keep a broad solid center and specify the widget's minimum separately.
        tile=(256,128) if size[0]>=36 else (128,64)
        base = shape(fill, outline, radius, tile)
        variants = [(state, shape(color, edge, radius, tile)) for state, color, edge in states]
        style.element_create(name, 'image', base, *variants, border=border, padding=0,
                             width=size[0], height=size[1], sticky='nsew')

    # Bounded nine-slice tiles; no per-frame image allocation or Canvas widgets.
    surface('Soft.Surface', COLORS['panel'], COLORS['panel_border'], radius=6, size=(36, 36), border=16)
    style.layout('Surface.TFrame', [('Soft.Surface', {'sticky': 'nsew'})])
    style.configure('Surface.TFrame', background=COLORS['bg'])
    for prefix, normal, edge, hover, pressed, disabled, ink in (
        ('', COLORS['button'], COLORS['border'], '#d5e2e8', '#cddde4', '#e0e7eb', COLORS['ink']),
        ('Primary.', COLORS['accent'], COLORS['accent'], '#087781', '#086f78', '#d5e0e5', 'white'),
        ('Stop.', '#ad333d', '#ad333d', '#a5323c', '#9d303a', '#e0e7eb', 'white'),
        ('Nav.', COLORS['nav'], COLORS['nav'], '#1b3a46', '#1d3f4b', COLORS['nav'], '#d0e1e8'),
    ):
        element = 'Soft.' + prefix + 'button'
        surface(element, normal, edge, states=(
            ('disabled', disabled, '#bacbd3' if prefix != 'Nav.' else disabled), ('pressed', pressed, edge),
            ('selected', '#24515f', '#24515f'), ('focus', normal, '#52a5ad'),
            ('active', hover, edge)))
        name = prefix + 'TButton'
        style.layout(name, [(element, {'sticky': 'nsew', 'children': [
            ('Button.padding', {'sticky': 'nsew', 'children': [('Button.label', {'sticky': 'nsew'})]})]})])
        style.configure(name, background=COLORS['panel'] if prefix != 'Nav.' else COLORS['nav'],
                        foreground=ink, padding=(10, 7), borderwidth=0, relief='flat', shiftrelief=0)
        style.map(name, background=[('disabled', COLORS['panel'] if prefix != 'Nav.' else COLORS['nav']),
                                    ('!disabled', COLORS['panel'] if prefix != 'Nav.' else COLORS['nav'])],
                  foreground=[('disabled', '#6b818d'), ('selected', 'white')])
    # Transparent image corners must match the actual enclosing surface.
    for base in ('TButton','Primary.TButton','Stop.TButton'):
        style.configure('Shell.'+base,background=COLORS['bg'])
        style.map('Shell.'+base,background=[('disabled',COLORS['bg']),('!disabled',COLORS['bg'])])
    style.configure('Nav.TButton', padding=(15, 14), anchor='w')
    style.configure('Compact.TButton', padding=(8, 3))
    style.configure('Compact.Primary.TButton', padding=(8, 3))

    surface('Soft.field', COLORS['field'], COLORS['border'], states=(
        ('disabled', '#e0e7eb', '#bacbd3'), ('focus', COLORS['field'], COLORS['accent']),
        ('readonly', '#e1e9ed', COLORS['border'])), radius=3)
    for control in ('Entry', 'Spinbox', 'Combobox'):
        def replace(items):
            return [('Soft.field' if name == control+'.field' else name,
                     {key: replace(value) if key == 'children' else value for key, value in options.items()})
                    for name, options in items]
        style.layout('T'+control, replace(style.layout('T'+control)))
        style.configure('T'+control, background=COLORS['panel'], fieldbackground=COLORS['field'], borderwidth=0,
                        bordercolor=COLORS['border'], lightcolor=COLORS['panel'], darkcolor=COLORS['panel'],
                        arrowcolor=COLORS['muted'], arrowsize=13, relief='flat')
        style.map('T'+control, bordercolor=[('focus', COLORS['accent'])],
                  background=[('disabled', COLORS['panel']), ('!disabled', COLORS['panel'])],
                  fieldbackground=[('disabled','#e0e7eb'),('readonly','#e1e9ed')])

    def arrow(direction, color, size):
        im=Image.new('RGBA',(size[0]*3,size[1]*3));draw=ImageDraw.Draw(im)
        x,y=size[0]*1.5,size[1]*1.5
        points=[(x-8,y-3),(x+8,y-3),(x,y+5)] if direction=='down' else [(x-8,y+3),(x+8,y+3),(x,y-5)]
        draw.polygon(points,fill=color)
        photo=ImageTk.PhotoImage(im.resize(size,Image.Resampling.LANCZOS),master=owner);images.append(photo);return photo
    for control,direction,size in [('Combobox','down',(20,20)),('Spinbox','up',(16,10)),('Spinbox','down',(16,10))]:
        style.element_create('Soft.'+control+'.'+direction+'arrow','image',arrow(direction,COLORS['muted'],size),
                             ('disabled',arrow(direction,'#a8b7be',size)),sticky='')
    style.layout('TCombobox',[('Soft.field',{'sticky':'nsew','children':[
        ('Soft.Combobox.downarrow',{'side':'right','sticky':'ns'}),
        ('Combobox.padding',{'sticky':'nsew','children':[('Combobox.textarea',{'sticky':'nsew'})]})]})])
    style.layout('TSpinbox',[('Soft.field',{'sticky':'nsew','children':[
        ('null',{'side':'right','children':[
            ('Soft.Spinbox.uparrow',{'side':'top','sticky':'e'}),
            ('Soft.Spinbox.downarrow',{'side':'bottom','sticky':'e'})]}),
        ('Spinbox.padding',{'sticky':'nsew','children':[('Spinbox.textarea',{'sticky':'nsew'})]})]})])

    surface('Soft.tab', '#cbd9e1', '#9eb3bf', states=(
        ('disabled', '#dce5ea', '#bacbd3'), ('focus', COLORS['panel'], COLORS['accent']),
        ('selected', COLORS['panel'], COLORS['accent']), ('active', '#d8e5e9', COLORS['border'])), radius=4)
    style.layout('TNotebook', [('Notebook.padding', {'sticky': 'nsew'})])
    style.layout('TNotebook.Tab', [('Soft.tab', {'sticky': 'nsew', 'children': [
        ('Notebook.padding', {'sticky': 'nsew', 'children': [
            ('Notebook.label', {'sticky': 'nsew'})]})]})])
    style.configure('TNotebook', borderwidth=0, tabmargins=(0, 0, 0, 3))
    style.configure('TNotebook.Tab', padding=(14, 7),font=('Noto Sans CJK KR',11),background=COLORS['bg'])
    # Clam maps selected tabs to {6 4 6 2}, shrinking them on every click.
    style.map('TNotebook.Tab',padding=[],font=[])
    style.map('TNotebook.Tab',background=[('selected',COLORS['bg']),('!selected',COLORS['bg'])])
    style.configure('Card.TNotebook',background=COLORS['panel'])
    style.configure('Card.TNotebook.Tab',background=COLORS['panel'])
    style.map('Card.TNotebook.Tab',background=[('selected',COLORS['panel']),('!selected',COLORS['panel'])])
    style.map('TNotebook.Tab', foreground=[('disabled', '#73838c'), ('selected', COLORS['accent'])])

    # Twelve-pixel hit area around a six-pixel thumb; native wheel/drag/page bindings.
    for orient in ('Vertical', 'Horizontal'):
        vertical = orient == 'Vertical'
        size = (12, 28) if vertical else (28, 12)
        def thumb(color):
            im = Image.new('RGBA', (size[0]*3, size[1]*3))
            d = ImageDraw.Draw(im)
            box = (9, 3, 26, size[1]*3-4) if vertical else (3, 9, size[0]*3-4, 26)
            d.rounded_rectangle(box, radius=9, fill=color)
            photo = ImageTk.PhotoImage(im.resize(size, Image.Resampling.LANCZOS), master=owner)
            images.append(photo)
            return photo
        name = 'Soft.'+orient+'.Scrollbar.thumb'
        style.element_create(name, 'image', thumb('#7c98a5'), ('pressed', thumb('#527d88')),
                             ('active', thumb('#7d9da6')), border=(0, 5) if vertical else (5, 0),
                             padding=0, sticky='ns' if vertical else 'ew')
        style.layout(orient+'.TScrollbar', [(orient+'.Scrollbar.trough', {'sticky': 'nsew', 'children': [
            (name, {'sticky': 'ns' if vertical else 'ew'})]})])
        style.configure(orient+'.TScrollbar', troughcolor=COLORS['panel'], borderwidth=0,
                        bordercolor=COLORS['panel'], lightcolor=COLORS['panel'], darkcolor=COLORS['panel'], arrowsize=12,
                        background=COLORS['panel'], relief='flat')

    track = shape('#acbfc9', radius=3, size=(128, 6), inset=0)
    knob = shape(COLORS['accent'], radius=7, size=(16, 16))
    focused = shape(COLORS['accent'], '#72bec5', radius=7, size=(16, 16))
    inactive = shape('#aebfc5', radius=7, size=(16, 16))
    style.element_create('Soft.Horizontal.Scale.trough', 'image', track, border=3, padding=0, width=24,height=6,sticky='ew')
    style.element_create('Soft.Horizontal.Scale.slider', 'image', knob, ('disabled', inactive),
                         ('focus', focused), ('active', focused))
    style.layout('Horizontal.TScale', [('Soft.Horizontal.Scale.trough', {'sticky': 'ew', 'children': [
        ('Soft.Horizontal.Scale.slider', {'side': 'left', 'sticky': ''})]})])
    style.configure('Horizontal.TScale', background=COLORS['panel'])
    style.map('Horizontal.TScale', background=[('disabled',COLORS['panel']),('!disabled',COLORS['panel'])])
    style.configure('Treeview', background=COLORS['field'],fieldbackground=COLORS['field'],borderwidth=0, relief='flat', bordercolor=COLORS['panel_border'])
    style.configure('Treeview.Heading', relief='flat', borderwidth=0, background=COLORS['heading'],
                    bordercolor=COLORS['heading'], lightcolor=COLORS['heading'], darkcolor=COLORS['heading'], foreground=COLORS['ink'])
    style.map('Treeview.Heading', background=[('active', '#bdcfd8')])
    style.configure('TCheckbutton', background=COLORS['panel'], indicatorbackground=COLORS['field'],
                    indicatorforeground=COLORS['accent'])
    style.configure('Jig.TCheckbutton',background=COLORS['panel'])
    style.configure('TRadiobutton', background=COLORS['panel'])
