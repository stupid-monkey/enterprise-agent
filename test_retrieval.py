from app.rag.retriever import (
    KnowledgeRetriever
)


retriever = KnowledgeRetriever()


queries = [
    "工作3年有多少天年假？",
    "报销需要经过谁审批？",
    "公司的食堂今天吃什么？",
]


for query in queries:

    print("\n======================")
    print("Query:", query)

    results = retriever.search(
        query=query,
        top_k=2
    )

    for index, item in enumerate(
        results
    ):

        print(
            f"\nResult {index + 1}"
        )

        print(
            "Distance:",
            item["distance"]
        )

        print(
            "Metadata:",
            item["metadata"]
        )

        print(
            "Content:"
        )

        print(
            item["content"]
        )