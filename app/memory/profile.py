import json
from pathlib import Path


PROFILE_PATH = Path("data/user_profile.json")


class UserProfileMemory:
    def __init__(self):

        PROFILE_PATH.parent.mkdir(exist_ok=True)

        self.path = PROFILE_PATH

        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self.profile = json.load(f)

        else:
            self.profile = {}

    def update(self, key, value):

        self.profile[key] = value

        self.save()

    def get(self, key, default=None):

        return self.profile.get(key, default)

    def save(self):

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.profile, f, ensure_ascii=False, indent=4)

    def get_all(self):

        return self.profile
