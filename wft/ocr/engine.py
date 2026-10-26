"""Reading text off a crop.

Two backends, chosen at import time:

* `tesserocr` — the Tesseract C API, kept loaded between calls.
* the `tesseract` command line — always available, but pays process startup and
  a reload of the language model on every single call.

For a reward screen (four crops) the difference is small enough not to matter.
For a video import (hundreds of tiles) it decides whether the job takes minutes
or tens of minutes, which is why the fast path exists at all. The CLI backend
is a genuine fallback, not a placeholder: the tool works without the AUR package.

Preprocessing is deliberately **not** applied by default. Measured on real
material, raw crops beat Otsu-thresholded ones on the reward screen, where the
background is live gameplay; the inventory screen is the opposite case. So the
caller decides, per screen, based on measurement rather than a rule.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

PageMode = Literal[6, 7, 8, 10]

#: Page segmentation modes, named for what they actually assume.
PSM_BLOCK = 6      # a uniform block of text; correct for names that wrap
PSM_LINE = 7       # exactly one line
PSM_WORD = 8       # a single word
PSM_CHAR = 10      # a single character; used for the quantity badge

_BACKEND: str


try:  # pragma: no cover - depends on what is installed
    import tesserocr  # type: ignore
    from PIL import Image  # noqa: F401

    _BACKEND = "tesserocr"
except ImportError:  # pragma: no cover
    tesserocr = None  # type: ignore
    _BACKEND = "cli" if shutil.which("tesseract") else "none"


def backend() -> str:
    """Which backend is active: 'tesserocr', 'cli' or 'none'."""
    return _BACKEND


class OcrUnavailable(RuntimeError):
    pass


@dataclass
class Reading:
    text: str
    confidence: float | None = None


class Engine:
    """A reusable OCR engine.

    Instantiate once and reuse. With `tesserocr` that keeps the language model
    resident; with the CLI backend the instance is stateless, so the same code
    path works either way.
    """

    def __init__(self, language: str = "eng", psm: PageMode = PSM_BLOCK,
                 allowlist: str | None = None):
        self.language = language
        self.psm = psm
        self.allowlist = allowlist
        self._api = None

        if _BACKEND == "none":
            raise OcrUnavailable(
                "no OCR backend: install 'tesseract' and 'tesseract-data-eng', "
                "optionally 'tesserocr' for the faster in-process API")

        if _BACKEND == "tesserocr":
            self._api = tesserocr.PyTessBaseAPI(lang=language, psm=psm)  # type: ignore
            if allowlist:
                self._api.SetVariable("tessedit_char_whitelist", allowlist)

    def close(self) -> None:
        if self._api is not None:
            self._api.End()
            self._api = None

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    @staticmethod
    def _as_pil(image):
        """Accept a PIL image or an OpenCV BGR array.

        The capture and video paths produce numpy arrays, the fixtures produce
        PIL images. Converting here rather than at every call site keeps the
        conversion in one place and out of the pipeline code.
        """
        if hasattr(image, "save"):
            return image
        from PIL import Image

        array = image
        if array.ndim == 3 and array.shape[2] == 3:
            array = array[:, :, ::-1]      # BGR to RGB
        return Image.fromarray(array)

    def read(self, image, psm: PageMode | None = None) -> Reading:
        """Read one image, given as a PIL image or an OpenCV array."""
        image = self._as_pil(image)
        mode = psm if psm is not None else self.psm
        if _BACKEND == "tesserocr":
            return self._read_api(image, mode)
        return self._read_cli(image, mode)

    def _read_api(self, image, psm: PageMode) -> Reading:
        api = self._api
        assert api is not None
        if psm != self.psm:
            api.SetPageSegMode(psm)
        api.SetImage(image)
        text = api.GetUTF8Text().strip()
        confidence = api.MeanTextConf()
        if psm != self.psm:
            api.SetPageSegMode(self.psm)
        return Reading(text, float(confidence) if confidence >= 0 else None)

    def _read_cli(self, image, psm: PageMode) -> Reading:
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        command = ["tesseract", "stdin", "stdout", "--psm", str(psm), "-l", self.language]
        if self.allowlist:
            command += ["-c", f"tessedit_char_whitelist={self.allowlist}"]
        result = subprocess.run(command, input=buffer.getvalue(),
                                capture_output=True, timeout=30)
        return Reading(result.stdout.decode("utf-8", "replace").strip(), None)


def read_lines(reading: Reading) -> str:
    """Collapse a multi-line reading into one string.

    A wrapped name arrives as separate lines ("Lavos Prime Neuroptics" /
    "Blueprint"). Joining with a space reconstructs what the player sees, which
    is what the candidate list is matched against.
    """
    return " ".join(part.strip() for part in reading.text.splitlines() if part.strip())
