"""Build a reproducible public DOCX with images from one pinned Apache commit."""

import io
from pathlib import Path
from urllib.request import urlopen

from docx import Document
from docx.shared import Inches
from PIL import Image


COMMIT = "21968bcad8a2524bfa0304d7473503a0feee010f"
BASE = f"https://raw.githubusercontent.com/apache/dolphinscheduler/{COMMIT}/docs/img/"
IMAGES = (
    ("系统架构图", "architecture-1.3.0.jpg",
     "系统架构由 MasterServer、WorkerServer 和数据库等组件构成。"),
    ("启动流程图", "process-start-flow-1.3.0.png",
     "启动流程图展示服务启动与任务调度的主要步骤。"),
)
OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "evaluation" / "dolphinscheduler_visual_source.docx"


def main() -> None:
    document = Document()
    document.add_heading("Apache DolphinScheduler 公开架构图片资料", 0)
    document.add_paragraph("公开项目技术分享样例，不代表本企业项目实施成果。")
    document.add_paragraph(f"固定来源提交：{COMMIT}")
    for heading, filename, description in IMAGES:
        url = BASE + filename
        with urlopen(url, timeout=15) as response:
            content = response.read(4_000_001)
            if len(content) > 4_000_000:
                raise ValueError("IMAGE_TOO_LARGE")
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in ("JPEG", "PNG") or image.width < 400 or image.height < 200:
                raise ValueError("INVALID_SOURCE_IMAGE")
        document.add_heading(heading, level=1)
        document.add_paragraph(description)
        document.add_picture(io.BytesIO(content), width=Inches(5.8))
    document.core_properties.comments = f"Apache DolphinScheduler docs/img, commit {COMMIT}; see evaluation record."
    document.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
