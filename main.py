from app.agent.core import EnterpriseAgent


def main():

    print("Enterprise Agent starting...")

    try:
        agent = EnterpriseAgent()

    except Exception as error:
        print("\n[Startup Error]")

        print(f"{type(error).__name__}: {error}")

        return

    print("\nEnterprise Agent ready.")

    print("输入 quit 退出，reset 清空短期对话记忆。\n")

    while True:
        try:
            user_input = input("User: ").strip()

        except KeyboardInterrupt:
            print("\n\nAgent: 程序已退出。")

            break

        if not user_input:
            continue

        if user_input.lower() == "quit":
            print("\nAgent: 再见。")

            break

        if user_input.lower() == "reset":
            agent.reset()

            print("\nAgent: 短期对话记忆已重置。\n")

            continue

        try:
            answer = agent.run(user_input)

            print(f"\nAgent: {answer}\n")

        except Exception as error:
            print("\n[Unexpected Error]")

            print(f"{type(error).__name__}: {error}")

            print("\nAgent: 系统发生未处理异常，但服务仍在运行，请重试。\n")


if __name__ == "__main__":
    main()
