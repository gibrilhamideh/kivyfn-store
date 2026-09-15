from enum import Enum


class OpCode(str, Enum):
    SET = "set"
    SETROW = "setrow"
    INSERT = "insert"
    REMOVE = "remove"

    def __str__(self) -> str:
        return self.value
