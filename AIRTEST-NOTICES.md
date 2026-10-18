# Optional offline image backend

The image backend uses selected, unmodified Airtest 1.4.3 image matching source
from https://github.com/AirtestProject/Airtest, licensed under Apache License 2.0.
Its `aircv/__init__.py` is replaced with an empty package initializer so importing
the matcher does not load the full Airtest image/device package. The original
license is included as `image_backend_runtime/AIRTEST-LICENSE.txt` in bundles.

Only `aircv/template_matching.py`, `aircv/utils.py`, `aircv/cal_confidence.py`,
`aircv/error.py`, the logger, version module, and package initializers are included.
No Airtest device drivers, capture/input APIs, ADB, iOS helpers, network client,
reporting, or executable tools are included. Source archive SHA-256:
`6208e83ca8d3618e32b8eee23b3e857a0077cd59accf158dd567a81df2a3b84c`.

The offline runtime pins NumPy 2.2.6, OpenCV Python Headless 4.12.0.88, and Pillow
11.3.0 for CPython 3.13 x64. Each wheel's license files and metadata are preserved.
NumPy is BSD-3-Clause; OpenCV is Apache-2.0 and its Python distribution includes
third-party notices; Pillow uses the HPND/PIL license. Consult the bundled license
files for dependencies such as OpenBLAS, image codecs, and FFmpeg.

The full upstream Airtest package pins older NumPy/OpenCV versions. This project
tests only the selected image matching modules with the versions above; it does
not claim full Airtest or device API compatibility with Python 3.13.

`tools/prepare_image_backend.py` builds from checksum-pinned local archives without
executing setup scripts or using a package installer. Runtime calls read supplied
PNG bytes only and do not download anything or send pixels to a network service.
The MCP's fresh capture, target ownership, geometry, click offset, RGB similarity,
and distinct-candidate checks remain required around this optional backend.

The production path runs the image matcher in a bounded, hidden Python child
process. NumPy/OpenCV are imported on that child's main thread, never on the MCP
server's request worker. A startup hang, crash, or cancellation terminates only
that owned child and does not dispatch desktop input. Requests and results are
local temporary JSON files; child standard streams do not share MCP stdout.

Measurements including this isolation startup overhead favor the existing native
matcher for the reviewed cases. Native matching therefore remains first for all
image sizes; Airtest is attempted only after a genuine `not_found`. Ambiguity and
low-detail rejections are final. In-process microbenchmark timings must not be
presented as production or end-to-end workflow speed improvements.
