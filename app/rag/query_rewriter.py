from app.llm.client import (
    create_llm_client,
    MODEL_NAME,
)


class QueryRewriter:
    def __init__(self):

        self.llm = create_llm_client()

    def rewrite(self, query: str) -> str:

        prompt = f"""
你是企业知识库检索优化助手。

你的任务：
将用户问题改写成适合企业内部知识库检索的关键词查询。


要求：

1. 保留用户真实意图，不改变问题含义。

2. 提取核心业务主题。

3. 补充企业制度文档中可能出现的表达。

4. 如果问题涉及以下场景，必须补充对应业务关键词：

- 到账、付款、钱什么时候收到：
  补充：
  报销、费用、财务、付款、到账、打款、审批流程

- 假期：
  补充：
  年假、休假、员工福利、制度

- 奖金：
  补充：
  年终奖、绩效、薪酬福利、奖金政策

- 审批流程：
  补充：
  申请、审批、审核、流程、制度


5. 输出多个检索短语，用空格分隔。

6. 不回答问题，只生成检索query。


用户问题：

{query}


输出检索query：
"""

        response = self.llm.chat.completions.create(
            model=MODEL_NAME, messages=[{"role": "user", "content": prompt}]
        )

        rewritten = response.choices[0].message.content

        return rewritten.strip()
