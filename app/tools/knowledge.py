from app.rag.retriever import KnowledgeRetriever
from app.config import RETRIEVAL_DISTANCE_THRESHOLD


class KnowledgeSearchTool:
    def __init__(self):
        self.retriever = KnowledgeRetriever()

    def search(self, query: str):

        results = self.retriever.search(query=query, top_k=3)

        if not results or results[0]["distance"] > RETRIEVAL_DISTANCE_THRESHOLD:
            return {"status": "NO_RELIABLE_KNOWLEDGE", "evidence": []}

        evidence = []

        for result in results:
            metadata = result["metadata"]

            evidence.append(
                {
                    "source": metadata["source"],
                    "section": metadata["section"],
                    "distance": round(result["distance"], 4),
                    "content": result["content"],
                }
            )

        return {"status": "SUCCESS", "evidence": evidence}
