from app.rag.retriever import KnowledgeRetriever


retriever = KnowledgeRetriever()


test_cases = [

    # ======================
    # 年假类：应该命中 annual_leave
    # ======================

    {
        "query": "工作3年有多少天年假？",
        "expected": "annual_leave",
    },

    {
        "query": "工作满5年可以休几天年假？",
        "expected": "annual_leave",
    },

    {
        "query": "员工的年假制度是什么？",
        "expected": "annual_leave",
    },


    # ======================
    # 报销类：应该命中 reimbursement
    # ======================

    {
        "query": "报销需要经过谁审批？",
        "expected": "reimbursement",
    },

    {
        "query": "员工报销的流程是什么？",
        "expected": "reimbursement",
    },

    {
        "query": "财务审核后多久到账？",
        "expected": "reimbursement",
    },


    # ======================
    # OOD：知识库里没有答案
    # ======================

    {
        "query": "公司的食堂今天吃什么？",
        "expected": None,
    },

    {
        "query": "公司几点下班？",
        "expected": None,
    },

    {
        "query": "公司有没有健身房？",
        "expected": None,
    },

    {
        "query": "今年年终奖是多少？",
        "expected": None,
    },

]


positive_distances = []
negative_distances = []


print("\n================ Retrieval Evaluation ================\n")


for case in test_cases:

    query = case["query"]
    expected = case["expected"]

    result = retriever.search(
        query=query,
        top_k=1
    )[0]

    predicted = (
        result["metadata"]["category"]
    )

    distance = result["distance"]


    print("Query:", query)

    print(
        "Expected:",
        expected
    )

    print(
        "Retrieved:",
        predicted
    )

    print(
        "Distance:",
        round(distance, 4)
    )


    if expected is None:

        negative_distances.append(
            distance
        )

        print(
            "Type: OOD"
        )

    else:

        positive_distances.append(
            distance
        )

        print(
            "Category Correct:",
            predicted == expected
        )


    print(
        "------------------------------------------------"
    )


# ==========================
# 统计距离
# ==========================

max_positive = max(
    positive_distances
)

min_negative = min(
    negative_distances
)


print("\n================ Summary ================\n")

print(
    "正确知识问题最大 distance:",
    round(max_positive, 4)
)

print(
    "OOD 问题最小 distance:",
    round(min_negative, 4)
)


# ==========================
# 是否存在可分阈值
# ==========================

if max_positive < min_negative:

    threshold = (
        max_positive
        +
        min_negative
    ) / 2

    print(
        "\n存在初步可分阈值。"
    )

    print(
        "建议 threshold:",
        round(threshold, 4)
    )

else:

    print(
        "\n正负样本 distance 出现重叠。"
    )

    print(
        "单纯依靠 distance threshold 不够，"
        "需要增加 reranker / hybrid search / 更多评估数据。"
    )