import json
from pathlib import Path


SUMMARY_PATH = Path("data/conversation_summary.json")


class ConversationSummaryMemory:
    def __init__(self):

        SUMMARY_PATH.parent.mkdir(exist_ok=True)

        self.path = SUMMARY_PATH

        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

                self.summary = data.get("summary", "")

        else:
            self.summary = ""

    def get(self):

        return self.summary

    def update(self, summary: str):

        self.summary = summary

        self.save()

    def save(self):

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"summary": self.summary}, f, ensure_ascii=False, indent=4)
