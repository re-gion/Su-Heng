"""随 Python 包交付的品牌资源；内嵌后 HTML、PDF 与证据包均可离线显示。"""

from importlib.resources import files
from urllib.parse import quote

_ASSETS = files("yuqing.render").joinpath("assets")
LOGO_DATA_URI = "data:image/svg+xml," + quote(
    _ASSETS.joinpath("suheng-lockup.svg").read_text(encoding="utf-8"), safe=""
)
FAVICON_DATA_URI = "data:image/svg+xml," + quote(
    _ASSETS.joinpath("favicon.svg").read_text(encoding="utf-8"), safe=""
)
