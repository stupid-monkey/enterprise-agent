from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)


class RuntimeGuard:
    def call_llm(
        self,
        llm,
        **kwargs,
    ):
        try:
            response = llm.chat.completions.create(**kwargs)

            return True, response

        except RateLimitError as error:
            print(f"[Error][LLM][RateLimit] {error}")

            return (
                False,
                "LLM 服务当前请求过于频繁，请稍后再试。",
            )

        except APITimeoutError as error:
            print(f"[Error][LLM][Timeout] {error}")

            return (
                False,
                "LLM 服务响应超时，请稍后重试。",
            )

        except APIConnectionError as error:
            print(f"[Error][LLM][Connection] {error}")

            return (
                False,
                "暂时无法连接 LLM 服务，请检查网络后重试。",
            )

        except APIStatusError as error:
            status_code = getattr(
                error,
                "status_code",
                None,
            )

            print(f"[Error][LLM][APIStatus] status={status_code}, error={error}")

            if status_code == 402:
                return (
                    False,
                    "LLM 服务当前余额不足，暂时无法完成请求。",
                )

            if status_code == 401:
                return (
                    False,
                    "LLM API 认证失败，请检查 API Key。",
                )

            if status_code == 403:
                return (
                    False,
                    "LLM API 当前没有权限执行该请求。",
                )

            if status_code >= 500:
                return (
                    False,
                    "LLM 服务当前异常，请稍后重试。",
                )

            return (
                False,
                "LLM 请求失败，请稍后重试。",
            )

        except Exception as error:
            print(f"[Error][LLM][Unknown] {type(error).__name__}: {error}")

            return (
                False,
                "LLM 服务发生未知异常，请稍后重试。",
            )

    def call_tool(
        self,
        tool_name,
        tool_function,
        *args,
        **kwargs,
    ):
        try:
            result = tool_function(
                *args,
                **kwargs,
            )

            return True, result

        except ZeroDivisionError as error:
            print(f"[Error][Tool][{tool_name}] {error}")

            return (
                False,
                "计算失败：除数不能为 0。",
            )

        except Exception as error:
            print(f"[Error][Tool][{tool_name}] {type(error).__name__}: {error}")

            return (
                False,
                f"工具 {tool_name} 执行失败。",
            )
