from typing import Any


class BaseField:
    def __init__(self) -> None:
        self.name: str
        self.defualt: Any = None

    def __set_name__(self, owner, name):
        self.name = name

    @property
    def metadata(self) -> dict:
        return {}

    def challenge(self, value):
        pass
