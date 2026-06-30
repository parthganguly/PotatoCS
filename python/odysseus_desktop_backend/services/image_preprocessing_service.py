from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IMAGE_PREPROCESS_VERSION = "image-preprocess-v1"
SUPPORTED_IMAGE_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
}
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_ORIGINAL_BYTES = 25 * 1024 * 1024
MAX_DECODED_PIXELS = 40_000_000
THUMBNAIL_MAX_EDGE = 384
VISION_MAX_EDGE = 1024
VISION_MAX_PIXELS = 1_100_000
VISION_JPEG_QUALITY = 85
OCR_MAX_EDGE = 2000
ALPHA_BACKGROUND_RGB = (245, 245, 240)
ALPHA_BACKGROUND_LABEL = "neutral-245-245-240"


class ImagePreprocessingError(ValueError):
    category = "image_preprocessing_error"


class ImageValidationError(ImagePreprocessingError):
    category = "invalid_image"


@dataclass(frozen=True)
class DecodedImage:
    image: Any
    format: str
    mime_type: str
    extension: str
    original_width: int
    original_height: int
    width: int
    height: int
    original_mode: str
    color_mode: str
    original_pixel_count: int
    pixel_count: int
    has_alpha: bool
    exif_orientation: str
    orientation_normalized: bool
    pillow_version: str

    def metadata(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "mime_type": self.mime_type,
            "extension": self.extension,
            "original_width": self.original_width,
            "original_height": self.original_height,
            "width": self.width,
            "height": self.height,
            "original_color_mode": self.original_mode,
            "color_mode": self.color_mode,
            "original_pixel_count": self.original_pixel_count,
            "pixel_count": self.pixel_count,
            "has_alpha": self.has_alpha,
            "exif_orientation": self.exif_orientation,
            "orientation_normalized": self.orientation_normalized,
            "pillow_version": self.pillow_version,
        }


class ImagePreprocessingService:
    def decode(self, path: Path) -> DecodedImage:
        from PIL import Image, ImageOps, UnidentifiedImageError, __version__ as pillow_version

        Image.MAX_IMAGE_PIXELS = MAX_DECODED_PIXELS
        try:
            with Image.open(path) as raw:
                raw.load()
                image_format = str(raw.format or "").upper()
                if image_format not in SUPPORTED_IMAGE_FORMATS:
                    raise ImageValidationError("supported image formats are PNG, JPEG, and WebP")
                if bool(getattr(raw, "is_animated", False)) or int(getattr(raw, "n_frames", 1) or 1) > 1:
                    raise ImageValidationError("animated images are not supported")
                original_width = int(raw.width)
                original_height = int(raw.height)
                original_pixels = original_width * original_height
                if original_pixels <= 0:
                    raise ImageValidationError("image dimensions must be positive")
                if original_pixels > MAX_DECODED_PIXELS:
                    raise ImageValidationError(f"image exceeds {MAX_DECODED_PIXELS} decoded pixels")
                exif_orientation = ""
                try:
                    exif_orientation = str(raw.getexif().get(274) or "")
                except Exception:
                    exif_orientation = ""
                original_mode = str(raw.mode or "")
                transposed = ImageOps.exif_transpose(raw)
                if transposed.mode not in {"RGB", "RGBA", "LA", "L", "P"}:
                    transposed = transposed.convert("RGBA" if has_alpha_channel(transposed) else "RGB")
                mime, extension = SUPPORTED_IMAGE_FORMATS[image_format]
                return DecodedImage(
                    image=transposed.copy(),
                    format=image_format,
                    mime_type=mime,
                    extension=extension,
                    original_width=original_width,
                    original_height=original_height,
                    width=int(transposed.width),
                    height=int(transposed.height),
                    original_mode=original_mode,
                    color_mode=str(transposed.mode or ""),
                    original_pixel_count=original_pixels,
                    pixel_count=int(transposed.width) * int(transposed.height),
                    has_alpha=has_alpha_channel(raw) or has_alpha_channel(transposed),
                    exif_orientation=exif_orientation,
                    orientation_normalized=transposed.size != raw.size or exif_orientation not in {"", "1"},
                    pillow_version=pillow_version,
                )
        except UnidentifiedImageError as exc:
            raise ImageValidationError("file is not a decodable PNG, JPEG, or WebP image") from exc

    def write_derivatives(
        self,
        *,
        artifact_id: str,
        image: Any,
        original_hash: str,
        output_root: Path,
    ) -> dict[str, dict[str, Any]]:
        return {
            "thumbnail": self.write_thumbnail(artifact_id, image, original_hash, output_root / "thumbnails"),
            "vision_input": self.write_vision_input(artifact_id, image, original_hash, output_root / "vision"),
            "ocr_input": self.write_ocr_input(artifact_id, image, original_hash, output_root / "ocr"),
        }

    def write_thumbnail(self, artifact_id: str, image: Any, original_hash: str, output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        prepared, alpha_flattened = prepare_rgb(image, THUMBNAIL_MAX_EDGE)
        path = output_dir / f"{artifact_id}-thumbnail-{IMAGE_PREPROCESS_VERSION}.png"
        prepared.save(path, format="PNG", optimize=True)
        return derivative_metadata(
            path=path,
            kind="thumbnail",
            original_hash=original_hash,
            format_name="PNG",
            mime_type="image/png",
            color_mode=str(prepared.mode),
            max_edge=THUMBNAIL_MAX_EDGE,
            alpha_flattened=alpha_flattened,
        )

    def write_vision_input(self, artifact_id: str, image: Any, original_hash: str, output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        prepared, alpha_flattened = prepare_rgb(image, VISION_MAX_EDGE, max_pixels=VISION_MAX_PIXELS)
        path = output_dir / f"{artifact_id}-vision-{IMAGE_PREPROCESS_VERSION}.jpg"
        prepared.save(path, format="JPEG", quality=VISION_JPEG_QUALITY, optimize=True)
        return derivative_metadata(
            path=path,
            kind="vision_input",
            original_hash=original_hash,
            format_name="JPEG",
            mime_type="image/jpeg",
            color_mode=str(prepared.mode),
            max_edge=VISION_MAX_EDGE,
            max_pixels=VISION_MAX_PIXELS,
            jpeg_quality=VISION_JPEG_QUALITY,
            alpha_flattened=alpha_flattened,
        )

    def write_ocr_input(self, artifact_id: str, image: Any, original_hash: str, output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        prepared, alpha_flattened = prepare_rgb(image, OCR_MAX_EDGE)
        path = output_dir / f"{artifact_id}-ocr-{IMAGE_PREPROCESS_VERSION}.png"
        prepared.save(path, format="PNG", optimize=True)
        return derivative_metadata(
            path=path,
            kind="ocr_input",
            original_hash=original_hash,
            format_name="PNG",
            mime_type="image/png",
            color_mode=str(prepared.mode),
            max_edge=OCR_MAX_EDGE,
            alpha_flattened=alpha_flattened,
        )


def prepare_rgb(image: Any, max_edge: int, *, max_pixels: int | None = None) -> tuple[Any, bool]:
    from PIL import Image

    alpha = has_alpha_channel(image)
    if alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (*ALPHA_BACKGROUND_RGB, 255))
        prepared = Image.alpha_composite(background, rgba).convert("RGB")
    else:
        prepared = image.convert("RGB")
    prepared.thumbnail((max_edge, max_edge))
    if max_pixels and prepared.width * prepared.height > max_pixels:
        scale = (max_pixels / float(prepared.width * prepared.height)) ** 0.5
        next_size = (max(1, int(prepared.width * scale)), max(1, int(prepared.height * scale)))
        prepared = prepared.resize(next_size)
    return prepared, alpha


def has_alpha_channel(image: Any) -> bool:
    try:
        if "A" in image.getbands():
            return True
        if image.mode == "P" and "transparency" in getattr(image, "info", {}):
            return True
    except Exception:
        return False
    return False


def derivative_metadata(
    *,
    path: Path,
    kind: str,
    original_hash: str,
    format_name: str,
    mime_type: str,
    color_mode: str,
    max_edge: int,
    alpha_flattened: bool,
    max_pixels: int | None = None,
    jpeg_quality: int | None = None,
) -> dict[str, Any]:
    from PIL import Image

    with Image.open(path) as image:
        width = int(image.width)
        height = int(image.height)
    size_bytes = path.stat().st_size
    metadata: dict[str, Any] = {
        "kind": kind,
        "stored_path": str(path),
        "content_hash": file_sha256(path),
        "preprocessing_version": IMAGE_PREPROCESS_VERSION,
        "source_content_hash": original_hash,
        "format": format_name,
        "mime_type": mime_type,
        "width": width,
        "height": height,
        "pixels": width * height,
        "size_bytes": size_bytes,
        "max_edge": max_edge,
        "color_mode": color_mode,
        "alpha_flattened": alpha_flattened,
        "alpha_background": ALPHA_BACKGROUND_LABEL if alpha_flattened else "",
    }
    if max_pixels is not None:
        metadata["max_pixels"] = max_pixels
    if jpeg_quality is not None:
        metadata["jpeg_quality"] = jpeg_quality
    return metadata


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
