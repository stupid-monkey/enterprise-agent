import chromadb

from sentence_transformers import SentenceTransformer

from app.config import (
    DATA_DIR,
    CHROMA_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
)


def load_sections(file_path):
    with open(
        file_path,
        "r",
        encoding="utf-8"
    ) as f:
        lines = f.readlines()

    sections = []

    current_title = None
    current_lines = []

    for raw_line in lines:

        line = raw_line.strip()

        if not line:
            continue

        # 例如：
        # 公司报销制度：
        # 年假制度：
        if line.endswith("制度："):

            # 保存上一个章节
            if current_title is not None:

                sections.append(
                    {
                        "title": current_title,
                        "content": "\n".join(
                            current_lines
                        )
                    }
                )

            current_title = line
            current_lines = []

        else:
            current_lines.append(line)

    # 保存最后一个章节
    if current_title is not None:

        sections.append(
            {
                "title": current_title,
                "content": "\n".join(
                    current_lines
                )
            }
        )

    return sections


def main():

    source_file = (
        DATA_DIR / "company_policy.txt"
    )

    # =========================
    # 1. 文档解析
    # =========================

    sections = load_sections(
        source_file
    )

    print(
        f"解析到 {len(sections)} 个知识章节："
    )

    for section in sections:

        print(
            f"- {section['title']}"
        )


    # =========================
    # 2. 准备文档 + metadata
    # =========================

    documents = []

    metadatas = []

    ids = []


    for index, section in enumerate(
        sections
    ):

        document = (
            f"{section['title']}\n"
            f"{section['content']}"
        )

        documents.append(
            document
        )

        ids.append(
            f"policy_{index}"
        )


        # metadata
        if "年假" in section["title"]:
            category = "annual_leave"

        elif "报销" in section["title"]:
            category = "reimbursement"

        else:
            category = "general"


        metadatas.append(
            {
                "source":
                    "company_policy.txt",

                "section":
                    section["title"],

                "category":
                    category,
            }
        )


    # =========================
    # 3. Embedding
    # =========================

    print(
        f"加载 Embedding 模型："
        f"{EMBEDDING_MODEL}"
    )

    model = SentenceTransformer(
        EMBEDDING_MODEL
    )

    embeddings = model.encode(
        documents
    )


    # =========================
    # 4. Chroma
    # =========================

    client = (
        chromadb.PersistentClient(
            path=str(CHROMA_DIR)
        )
    )


    # 重建索引
    try:

        client.delete_collection(
            name=COLLECTION_NAME
        )

    except Exception:
        pass


    collection = (
        client.create_collection(
            name=COLLECTION_NAME
        )
    )


    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings.tolist(),
        metadatas=metadatas,
    )


    print(
        f"\n索引构建完成："
        f"{len(documents)} 个知识块"
    )


    # =========================
    # 5. 打印检查
    # =========================

    for doc_id, metadata in zip(
        ids,
        metadatas
    ):

        print(
            f"{doc_id} -> {metadata}"
        )


if __name__ == "__main__":
    main()