import chromadb

from sentence_transformers import SentenceTransformer

from app.config import (
    CHROMA_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    RETRIEVAL_DISTANCE_THRESHOLD,
)


class KnowledgeRetriever:
    def __init__(self):

        print(f"[Retriever] 加载 Embedding 模型：{EMBEDDING_MODEL}")

        # =========================
        # Embedding Model
        # =========================

        self.embedding_model = SentenceTransformer(EMBEDDING_MODEL)

        # =========================
        # ChromaDB
        # =========================

        self.client = chromadb.PersistentClient(path=str(CHROMA_DIR))

        self.collection = self.client.get_collection(name=COLLECTION_NAME)

    def _query_chroma(
        self,
        query: str,
        top_k: int,
    ) -> list[dict]:
        """
        最底层检索。

        只负责：
        query -> embedding -> Chroma

        不执行 threshold filtering。
        """

        query_embedding = self.embedding_model.encode(query).tolist()

        result = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=[
                "documents",
                "metadatas",
                "distances",
            ],
        )

        documents = result.get(
            "documents",
            [[]],
        )[0]

        metadatas = result.get(
            "metadatas",
            [[]],
        )[0]

        distances = result.get(
            "distances",
            [[]],
        )[0]

        results = []

        for (
            document,
            metadata,
            distance,
        ) in zip(
            documents,
            metadatas,
            distances,
        ):
            section = metadata.get("section", "") if metadata else ""

            print(f"[Retriever] distance={distance:.4f}, section={section}")

            results.append(
                {
                    "content": document,
                    "metadata": metadata or {},
                    "distance": float(distance),
                }
            )

        return results

    def search_raw(
        self,
        query: str,
        top_k: int = 3,
    ) -> list[dict]:
        """
        返回原始检索结果。

        不经过 distance threshold。

        主要用于：
        - Evaluation
        - Threshold calibration
        - Debugging
        """

        return self._query_chroma(
            query=query,
            top_k=top_k,
        )

    def search(
        self,
        query: str,
        top_k: int = 3,
        distance_threshold=None,
    ) -> list[dict]:
        """
        正式 Agent 使用的检索。

        默认使用配置文件中的 threshold。

        允许 Evaluation 临时指定 threshold。
        """

        if distance_threshold is None:
            distance_threshold = RETRIEVAL_DISTANCE_THRESHOLD

        raw_results = self._query_chroma(
            query=query,
            top_k=top_k,
        )

        filtered_results = []

        for result in raw_results:
            distance = result["distance"]

            if distance > distance_threshold:
                continue

            filtered_results.append(result)

        return filtered_results
