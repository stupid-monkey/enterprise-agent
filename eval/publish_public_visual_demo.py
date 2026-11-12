"""Publish the pinned public image source and create a reviewable local PPT task."""

import getpass
from pathlib import Path

import httpx


BASE = "http://127.0.0.1:8080/api/v1"
SOURCE = Path(__file__).resolve().parents[1] / "docs" / "evaluation" / "dolphinscheduler_visual_source.docx"


def main() -> None:
    password = getpass.getpass("Local manager account password: ")
    with httpx.Client(timeout=30.0) as client:
        login = client.post(f"{BASE}/auth/login", json={"username": "manager", "password": password})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        upload = client.post(f"{BASE}/documents/upload", headers=headers,
                             data={"title": "DolphinScheduler 公开架构图片资料",
                                   "area": "产品资料", "visibility": "内部公开"},
                             files={"file": (SOURCE.name, SOURCE.read_bytes(),
                                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        upload.raise_for_status()
        document_id = upload.json()["id"]
        published = client.post(f"{BASE}/documents/{document_id}/publish", headers=headers)
        published.raise_for_status()
        created = client.post(f"{BASE}/artifacts", headers=headers, json={
            "kind": "pptx", "title": "DolphinScheduler 公开架构图示例",
            "goal": "公开项目技术分享", "source_ids": [document_id],
            "outline": ["系统架构图", "启动流程图", "组件职责", "后续讨论"]})
        created.raise_for_status()
        item = created.json()
        matched = client.post(f"{BASE}/artifacts/{item['id']}/slide-visuals", headers=headers,
                              json={"draft_token": item["draft_token"]})
        matched.raise_for_status()
        visuals = matched.json()["slide_visuals"]
        print({"document_id": document_id, "artifact_id": item["id"],
               "visuals": [(entry["page_index"] + 1, entry["source"]) for entry in visuals]})


if __name__ == "__main__":
    main()
