"""Series identity, colors, and highlight pen widths (no Qt)."""

from __future__ import annotations

from dataclasses import dataclass

_COLOR_CYCLE = [
    "#00ff99",
    "#00c8ff",
    "#ffaa00",
    "#ff6699",
    "#cc77ff",
    "#eeff44",
    "#66ffcc",
    "#ff8844",
]


@dataclass(slots=True)
class ChannelStyle:
    color_hex: str
    pen_width: float = 1.5


class ChannelRegistry:
    def __init__(self) -> None:
        self._styles: dict[str, ChannelStyle] = {}
        self._color_index = 0

    def __contains__(self, name: str) -> bool:
        return name in self._styles

    def names(self) -> dict[str, ChannelStyle]:
        return self._styles

    def style(self, name: str) -> ChannelStyle | None:
        return self._styles.get(name)

    def add(self, name: str) -> ChannelStyle:
        if name in self._styles:
            return self._styles[name]
        c = _COLOR_CYCLE[self._color_index % len(_COLOR_CYCLE)]
        self._color_index += 1
        st = ChannelStyle(color_hex=c, pen_width=1.5)
        self._styles[name] = st
        return st

    def remove(self, name: str) -> None:
        self._styles.pop(name, None)

    def clear(self) -> None:
        self._styles.clear()
        self._color_index = 0

    def set_highlight(self, name: str, highlighted: bool) -> bool:
        st = self._styles.get(name)
        if st is None:
            return False
        st.pen_width = 4.0 if highlighted else 1.5
        return True
