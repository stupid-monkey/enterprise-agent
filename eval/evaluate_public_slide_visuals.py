"""Exercise source-image matching and PPTX export on the pinned public DOCX."""

import io
import json
from pathlib import Path

from pptx import Presentation

from app.api.artifacts import ArtifactRepository
from app.api.content import extract_text
from app.api.documents import DocumentRepository
from app.security.policy import Classification, Subject, UserRole, Visibility


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "evaluation" / "dolphinscheduler_visual_source.docx"
OUTPUT = ROOT / "docs" / "evaluation" / "dolphinscheduler_source_visual_demo.pptx"
REPORT = ROOT / "eval" / "public_slide_visual_results.json"


def main() -> None:
    subject = Subject("visual-eval", "demo-org", frozenset({"product"}),
                      Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)
    documents = DocumentRepository(":memory:")
    artifacts = ArtifactRepository(":memory:")
    try:
        content = SOURCE.read_bytes()
        file_type, extracted_text = extract_text(SOURCE.name, content)
        document_id = documents.create_upload(
            subject, title="DolphinScheduler 公开架构图片资料", filename=SOURCE.name,
            file_type=file_type, area="产品资料", visibility=Visibility.ORGANIZATION,
            content=content, extracted_text=extracted_text)
        assert documents.publish(subject, document_id)
        item = artifacts.create(subject, documents, kind="pptx", title="DolphinScheduler 架构",
                                goal="公开项目技术分享", source_ids=[document_id],
                                outline=["系统架构图", "启动流程图", "组件职责", "后续讨论"])
        assert item is not None
        matched = artifacts.generate_slide_visuals(subject, item["id"], documents,
                                                   item["draft_token"], provider=None)
        assert matched is not None
        visuals = matched["slide_visuals"]
        confirmed = artifacts.confirm(subject, item["id"], documents,
                                      item["outline"], item["draft_token"])
        assert confirmed is not None
        download = artifacts.download(subject, item["id"], documents)
        assert download is not None
        OUTPUT.write_bytes(download[1])
        deck = Presentation(io.BytesIO(download[1]))
        report = {"source": str(SOURCE.relative_to(ROOT)),
                  "output": str(OUTPUT.relative_to(ROOT)),
                  "source_commit": "21968bcad8a2524bfa0304d7473503a0feee010f",
                  "slides": len(deck.slides),
                  "visuals": [{"page": visual["page_index"] + 1,
                               "source": visual["source"],
                               "source_location": visual["source_location"]}
                              for visual in visuals],
                  "real_model_used": False,
                  "note": "Public source image retrieval and export check; not a content-quality score."}
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        artifacts._db.close()
        documents.close()


if __name__ == "__main__":
    main()
