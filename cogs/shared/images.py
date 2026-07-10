"""Image encoding, sizing, and compression helpers."""

import base64
import io
import mimetypes
import os

from PIL import Image


def encode_image_to_base64(image_path: str) -> str:
    """Encode an image file as a Base64 data URI."""
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream"
    with open(image_path, "rb") as image_file:
        base64_encoded_data = base64.b64encode(image_file.read()).decode("utf-8")
    return f"data:{mime_type};base64,{base64_encoded_data}"


def get_file_size_kb(file_path: str) -> float:
    """Return a file's size in kilobytes, or zero when it is missing."""
    if os.path.exists(file_path):
        return os.path.getsize(file_path) / 1024
    return 0.0


async def compress_image(image_path: str, max_size_kb: int = 250) -> str:
    """Compress an image below the requested size when possible."""
    try:
        original_size_kb = get_file_size_kb(image_path)
        print(f"  原始图片大小: {original_size_kb:.2f}KB")

        if original_size_kb <= max_size_kb:
            return image_path

        print(f"  开始压缩图片 (目标: <{max_size_kb}KB)")

        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode in ("RGBA", "LA"):
                    background.paste(img, mask=img.split()[-1])
                else:
                    background.paste(img)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            base_name = os.path.splitext(image_path)[0]
            compressed_path = f"{base_name}_compressed.jpg"

            quality = 85
            max_dimension = 1920
            buffer = io.BytesIO()

            for attempt in range(5):
                width, height = img.size
                if width > max_dimension or height > max_dimension:
                    ratio = min(max_dimension / width, max_dimension / height)
                    new_width = int(width * ratio)
                    new_height = int(height * ratio)
                    resized_img = img.resize(
                        (new_width, new_height), Image.Resampling.LANCZOS
                    )
                else:
                    resized_img = img

                buffer = io.BytesIO()
                resized_img.save(buffer, format="JPEG", quality=quality, optimize=True)
                buffer_size_kb = buffer.tell() / 1024

                print(f"  尝试 {attempt + 1}: 质量={quality}, 大小={buffer_size_kb:.2f}KB")

                if buffer_size_kb <= max_size_kb:
                    buffer.seek(0)
                    with open(compressed_path, "wb") as f:
                        f.write(buffer.read())
                    print(
                        f"  压缩成功: {original_size_kb:.2f}KB -> {buffer_size_kb:.2f}KB "
                        f"({(1 - buffer_size_kb / original_size_kb) * 100:.1f}%)"
                    )
                    return compressed_path

                if attempt < 2:
                    quality -= 10
                else:
                    max_dimension = int(max_dimension * 0.8)
                    quality = 75

            print(f"  无法压缩到{max_size_kb}KB以下，使用最佳尝试结果")
            buffer.seek(0)
            with open(compressed_path, "wb") as f:
                f.write(buffer.read())
            return compressed_path

    except Exception as e:
        print(f"  图片压缩失败: {e}")
        return image_path
