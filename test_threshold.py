from app.rag.retriever import KnowledgeRetriever


retriever = KnowledgeRetriever()


queries = [
    "工作3年有多少天年假？",
    "报销需要经过谁审批？",
    "公司的食堂今天吃什么？",
    "今年年终奖是多少？",
]


for query in queries:

    print("\n======================")
    print("Query:", query)

    results = retriever.search(
        query=query,
        top_k=1
    )


    if not results:

        print(
            "Result: 知识库中没有可靠的相关信息"
        )

        continue


    result = results[0]

    print(
        "Section:",
        result["metadata"]["section"]
    )

    print(
        "Distance:",
        round(
            result["distance"],
            4
        )
    )

    print(
        "Content:"
    )

    print(
        result["content"]
    )