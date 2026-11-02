"""Validate media before publishing a model-readable immutable artifact."""

from io import BytesIO
from hashlib import sha256
from oea.contracts.configuration import MODEL_IMAGE_MAX_BYTES, MODEL_IMAGE_MAX_PIXELS
from oea.contracts.errors import RuntimeFault, require

def digest(data):
    return sha256(data).hexdigest()

def image_type(path, paths=None):
    from PIL import Image, UnidentifiedImageError
    with (paths.open_read(str(path)) if paths else path.open("rb")) as stream:
        raw = stream.read(MODEL_IMAGE_MAX_BYTES + 1)
    require(len(raw) <= MODEL_IMAGE_MAX_BYTES, "IMAGE_TOO_LARGE", "Image input is limited to 5 MiB")
    try:
        with Image.open(BytesIO(raw)) as image:
            require(image.format in {"PNG", "JPEG", "WEBP"} and image.width * image.height <= MODEL_IMAGE_MAX_PIXELS,
                "IMAGE_INVALID", "Use a supported image with at most 20 million pixels")
            media = Image.MIME[image.format]
            image.verify()
            return media, digest(raw)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise RuntimeFault("IMAGE_INVALID", "Image content is invalid or unsupported") from None
