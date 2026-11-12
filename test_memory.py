from app.memory.conversation import (
    ConversationMemory
)


memory = ConversationMemory(
    system_prompt="You are an assistant.",
    max_turns=3,
)


for i in range(1, 6):

    memory.add_user(
        f"这是第{i}轮用户问题"
    )

    memory.add_assistant(
        f"这是第{i}轮助手回答"
    )


print(
    "\n当前 Memory：\n"
)


for message in memory.messages:

    print(
        message["role"],
        ":",
        message["content"]
    )