"""Passive overlays drawn over the game.

`drawing` paints the grids, `helper` is the separate process that owns the
overlay window, `client` is the tracker's side of it. Nothing here imports Qt
at package level, so the helper can choose its platform before Qt loads.
"""
