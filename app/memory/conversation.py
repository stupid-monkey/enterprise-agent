class ConversationMemory:

    def __init__(
        self,
        system_prompt: str,
        max_turns: int = 5,
    ):

        self.max_turns = max_turns

        self._messages = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]


    @property
    def messages(self) -> list[dict]:

        return self._messages


    def add_user(
        self,
        content: str,
    ):

        self._messages.append(
            {
                "role": "user",
                "content": content,
            }
        )

        self._trim()


    def add_assistant(
        self,
        content: str,
    ):

        self._messages.append(
            {
                "role": "assistant",
                "content": content,
            }
        )


    def add_raw(
        self,
        message: dict,
    ):

        self._messages.append(
            message
        )


    def reset(self):

        system_message = (
            self._messages[0]
        )

        self._messages = [
            system_message
        ]


    def _trim(self):

        user_indices = [
            index
            for index, message
            in enumerate(self._messages)
            if message.get("role") == "user"
        ]


        if (
            len(user_indices)
            <= self.max_turns
        ):
            return


        start_index = (
            user_indices[-self.max_turns]
        )


        system_message = (
            self._messages[0]
        )


        recent_messages = (
            self._messages[start_index:]
        )


        self._messages = [
            system_message,
            *recent_messages,
        ]